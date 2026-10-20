"""SPARKNET-FW — the host firewall is the rule the published-port decision needs.

The AI-stack manifest asserts the node's chat surface is *published but
firewalled* (`manifests/ai-stack.json`, `network_boundary`). A published port
without the rule is exactly the failure mode that clause exists to prevent, so
the rule must be committed next to the compose file and must not silently drift
from the ports the manifest claims to protect.

These tests read files — they prove the declared rule matches the declared
topology. That the rule is *loaded* on a real node (iptables-restore holds, the
unit survives a docker restart) is `DoD: confirm: user` and is recorded, not
claimed here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROLE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROLE_ROOT / "manifests" / "ai-stack.json"
SCRIPT = ROLE_ROOT / "scripts" / "sparknet-firewall.sh"
UNIT = ROLE_ROOT / "systemd" / "sparknet-firewall.service"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def _script_text() -> str:
    return SCRIPT.read_text()


def test_the_firewall_script_is_present_and_executable():
    assert SCRIPT.is_file(), "the manifest claims a SPARKNET-FW host firewall; it must ship"
    assert SCRIPT.stat().st_mode & 0o111, "sparknet-firewall.sh must be executable"


def test_protected_ports_match_the_manifest_exposed_ports():
    boundary = _manifest()["network_boundary"]
    # e.g. "8181 (webui)" -> "8181"
    exposed = {re.split(r"[^0-9]", e)[0] for e in boundary["exposed_ports"]}
    text = _script_text()
    match = re.search(r"PROTECTED_TCP=\(([^)]*)\)", text)
    assert match, "PROTECTED_TCP must be declared in the script"
    protected = set(match.group(1).split())
    assert protected == exposed, (
        f"the firewall protects {sorted(protected)} but the manifest exposes "
        f"{sorted(exposed)}; a published port with no rule is the failure mode"
    )


def test_allowed_sources_cover_the_manifest_allow_list():
    boundary = _manifest()["network_boundary"]
    allowed = " ".join(boundary["allowed_sources"])
    text = _script_text()
    # Loopback, LAN, overlay, docker bridges and link-local must each appear.
    for net in ("127.0.0.0/8", "192.168.178.0/24", "10.100.0.0/24", "172.16.0.0/12", "169.254.0.0/16"):
        assert net in text, f"the firewall does not allow {net}, which the manifest names"
    assert "loopback" in allowed and "LAN" in allowed and "overlay" in allowed


def test_the_firewall_does_not_widen_into_signing_or_overlay_material():
    """The script is about ports, not identity: no key, signer or state dir."""
    text = _script_text()
    for forbidden in ("operational.ed25519", "signer.sock", "SPARKNET_STATE_DIR", "/keys"):
        assert forbidden not in text, f"the firewall references {forbidden!r}; it must not"


def test_the_unit_calls_the_shim_and_orders_after_docker():
    unit = UNIT.read_text()
    assert "ExecStart=/usr/local/libexec/sparknet/sparknet-firewall install" in unit
    assert "After=docker.service" in unit, "DOCKER-USER is docker's chain; the rule must re-apply after docker starts"
    assert "PartOf=docker.service" in unit, (
        "a docker daemon restart recreates DOCKER-USER; PartOf re-applies the rules then too"
    )
    assert "WantedBy=multi-user.target" in unit


def test_the_script_exposes_install_status_and_flush():
    text = _script_text()
    for verb in ("install)", "flush)", "status)"):
        assert verb in text, f"the script must support the {verb.rstrip(')')} verb"


def test_install_verifies_itself_and_exposes_verify():
    """A boot that builds a half-state must fail, not report success.

    `install` is the only thing that runs at boot, and on a host missing the
    service — or after a docker restart drops the FORWARD jump — nothing else
    re-checks. So install verifies its own result and the script exposes a
    standalone `verify` verb the unit runs as a second assertion.
    """
    text = _script_text()
    assert "verify)" in text, "the firewall must expose a verify verb"
    assert "verify_rules" in text, "the verify implementation must exist"
    # install_rules must call verify_rules, or the build can succeed unverified.
    install_body = text.split("install_rules()", 1)[1].split("\n}", 1)[0]
    assert "verify_rules" in install_body, "install_rules must verify its own result"


def test_the_unit_runs_verify_after_install():
    unit = UNIT.read_text()
    assert "ExecStartPost=/usr/local/libexec/sparknet/sparknet-firewall verify" in unit, (
        "the unit must assert enforcement after install"
    )
