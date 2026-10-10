"""The node container — declared, and coherent as declared.

peer PLAN Step 2: the deployable opencode node. The image is built by
`make container-build` from the repo's own bundle; the compose wires three
facts the peer charter cares about: the tunnel outlives the app (anchor owns
the namespace, app attaches to it), `.state` is a host bind mount (never a
named volume), and nothing in the node requires a GPU.

These tests read the Dockerfiles, entrypoints, the compose file and the
preflight — they prove the *declared* wiring is coherent, no stronger and no
weaker. The image build and a compose run with no GPU are real build actions,
verified on the build host (which has docker); live tunnel connectivity and
`chattr +i` on a deployed node's filesystem are `DoD: confirm: user` and are
recorded rather than claimed (peer charter: no live WireGuard claims from this
host).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROLE_ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROLE_ROOT / "container"
APP_DOCKERFILE = CONTAINER / "Dockerfile"
ANCHOR_DOCKERFILE = CONTAINER / "anchor" / "Dockerfile"
ENTRYPOINT = CONTAINER / "entrypoint.sh"
ANCHOR_ENTRYPOINT = CONTAINER / "anchor" / "entrypoint.sh"
HEALTHCHECK = CONTAINER / "healthcheck.sh"
COMPOSE = ROLE_ROOT / "configs" / "container" / "docker-compose.yml"
PREFLIGHT = ROLE_ROOT / "scripts" / "sparknet-chattr-preflight.sh"

PINNED_OPENCODE = "1.18.35"
PINNED_WG = "0.0.20230223"


def _text(path: Path) -> str:
    return path.read_text()


def _code(path: Path) -> str:
    """Executable lines only — comments may *name* a thing (nvidia, latest) to
    forbid it; only real instructions count for the negative-token checks."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


# --- image: no GPU, pinned opencode, per-arch, correct layout -----------------


def test_app_image_declares_no_gpu():
    dockerfile = _code(APP_DOCKERFILE)
    for token in ("nvidia", "cuda", "cudnn", "NVIDIA", "gpus:", "nvidia-smi"):
        assert token not in dockerfile, (
            f"the node image must not reference {token!r}; GPU is a deploy-time "
            "decision made inside the inference service, never the node base"
        )


def test_app_image_pins_the_opencode_version():
    dockerfile = _text(APP_DOCKERFILE)
    assert f"ARG OPENCODE_VERSION={PINNED_OPENCODE}" in dockerfile, (
        "the image must pin the opencode version (default arg), never fetch latest"
    )
    assert "latest" not in _code(APP_DOCKERFILE).lower(), "a mutating tag has no place in a node image"


def test_app_image_fetches_opencode_per_architecture():
    dockerfile = _text(APP_DOCKERFILE)
    assert "ARG TARGETARCH" in dockerfile
    assert "amd64) asset=x64" in dockerfile, "x64 release asset for amd64"
    assert "arm64) asset=arm64" in dockerfile, "arm64 release asset for arm64"
    assert "opencode-linux-${asset}.tar.gz" in dockerfile, (
        "the asset suffix must come from TARGETARCH, never a fixed binary"
    )


def test_app_image_lays_out_the_bundle_at_the_contract_path():
    dockerfile = _text(APP_DOCKERFILE)
    # sparknet-env.sh computes SPARKNET_FABRIC_ROOT as dirname(vault root), so the
    # vault MUST live at /srv/sparknet/peer-node with contracts+shared as siblings.
    assert "/srv/sparknet" in dockerfile
    assert "/srv/sparknet/peer-node" in dockerfile
    assert "test -d /srv/sparknet/contracts" in dockerfile
    assert "test -d /srv/sparknet/shared" in dockerfile


def test_app_image_bakes_no_vault_path():
    dockerfile = _text(APP_DOCKERFILE)
    entrypoint = _text(ENTRYPOINT)
    for baked in ("VAULT_PATH=/", "SPARKNET_VAULT_ROOT=", ".env.example"):
        assert baked not in dockerfile and baked not in entrypoint, (
            f"the image/entrypoint must not bake {baked!r}; the entrypoint regenerates .env at boot"
        )


def test_app_image_includes_a_healthcheck_script_and_tini():
    dockerfile = _text(APP_DOCKERFILE)
    assert "ENTRYPOINT" in dockerfile and "tini" in dockerfile
    assert "sparknet-healthcheck" in dockerfile
    assert HEALTHCHECK.exists()


# --- entrypoint / healthcheck: regen .env, never path-baked ------------------


def test_entrypoint_regenerates_env_and_execs():
    entrypoint = _text(ENTRYPOINT)
    assert "sparknet-env-init.sh" in entrypoint, "the entrypoint must regenerate .env"
    assert 'exec "$@"' in entrypoint, "the entrypoint must exec the command"
    assert "SPARKNET_STATE_DIR" in entrypoint


def test_healthcheck_is_version_anchored_and_gpu_agnostic():
    healthcheck = _code(HEALTHCHECK)
    assert "opencode --version" in healthcheck
    assert "OPENCODE_VERSION" in healthcheck
    for token in ("nvidia", "gpu", "smi"):
        assert token not in healthcheck.lower(), f"health must not mention {token!r}: a node is healthy without a GPU"


def test_all_container_scripts_pass_bash_dot_n():
    for script in (ENTRYPOINT, ANCHOR_ENTRYPOINT, HEALTHCHECK, PREFLIGHT):
        subprocess.run(["bash", "-n", str(script)], check=True)


# --- anchor: built from source, userspace, never a host interface name -------


def test_anchor_is_built_from_source_and_pinned():
    dockerfile = _text(ANCHOR_DOCKERFILE)
    assert "github.com/WireGuard/wireguard-go" in dockerfile, (
        "the anchor must build wireguard-go from source, never an opaque prebuilt binary"
    )
    assert "0.0.20230223" in dockerfile, "the anchor must pin the exact wireguard-go line, never a moving tag"
    assert any(
        "FROM --platform=$BUILDPLATFORM golang" in line or "FROM golang" in line for line in dockerfile.splitlines()
    ), "a multi-stage source build is expected; the Go stage runs on BUILDPLATFORM"


def test_anchor_uses_its_own_interface_never_host_ones():
    tok = _text(ANCHOR_ENTRYPOINT)
    assert "wgcore" in tok, "anchor interface must be wgcore"
    for host_dev in ("-f wg0", "dev=wg0", "dev=wg1"):
        assert host_dev not in tok, "the anchor must never claim a host interface name"


def test_anchor_has_an_honest_inert_mode():
    tok = _text(ANCHOR_ENTRYPOINT)
    assert "-s" in tok, "the anchor must require a non-empty config"
    assert "inert" in tok, "without a rendered config the anchor must say so, not fake a tunnel"


# --- compose wiring: netns, bind mount, healthcheck --------------------------


def test_app_joins_the_anchor_namespace():
    app = _compose()["services"]["app"]
    assert app.get("network_mode") == "service:anchor", (
        "the app must attach to the anchor's namespace; otherwise recreating the "
        "app rewrites the tunnel lifecycle and the anchor is pointless"
    )
    assert app.get("depends_on") == ["anchor"]


def test_state_is_a_host_bind_mount_never_a_named_volume():
    app = _compose()["services"]["app"]
    volumes = app.get("volumes", [])
    assert len(volumes) == 1
    bind = volumes[0]
    assert bind.get("type") == "bind"
    assert bind["target"] == "/srv/sparknet/peer-node/.state"
    assert bind["source"].startswith("${SPARKNET_STATE_DIR")
    top = _compose()
    assert not top.get("volumes"), f"compose must define no named volumes: {list(top.get('volumes', []))}"


def test_anchor_has_tunnel_privilege_and_readonly_config():
    anchor = _compose()["services"]["anchor"]
    caps = set(anchor.get("cap_add", []))
    assert {"NET_ADMIN", "NET_RAW"} <= caps, caps
    assert "/dev/net/tun" in anchor.get("devices", []), anchor.get("devices")
    assert anchor["volumes"][0].get("read_only") is True


def test_app_healthcheck_uses_the_image_script():
    health = _compose()["services"]["app"]["healthcheck"]
    assert "sparknet-healthcheck" in str(health.get("test", []))


def test_no_service_claims_a_gpu_or_overlay_port():
    for name, service in _compose()["services"].items():
        assert "gpus" not in service, f"{name} must not declare gpus"
        reservations = service.get("deploy", {}).get("resources", {}).get("reservations", {})
        assert "devices" not in reservations, f"{name} must not reserve devices"
        assert "ports" not in service, f"{name} exposes no ports; the overlay is reached via the anchor"


# --- preflight: refuses unless immutability is provable ----------------------


def test_preflight_refuses_without_proof_and_answers_help():
    body = PREFLIGHT.read_text()
    assert "chattr +i" in body
    assert "--help" in body, "fabric rule: every script answers --help with rc 0 and no side effect"
    run = subprocess.run(["bash", str(PREFLIGHT), "--help"], capture_output=True, text=True)
    assert run.returncode == 0 and "Usage:" in run.stdout


# --- the compose parses, when this host has docker ---------------------------


def test_compose_config_parses_when_docker_is_available(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH; the file-reading assertions above still apply")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
        cwd=ROLE_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"docker compose rejects the node compose:\n{result.stdout}\n{result.stderr}"
