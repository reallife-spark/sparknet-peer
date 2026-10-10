"""The node's own AI stack — declared, and coherent as declared.

CLUSTER_INFERENCE_DESIGN.md adoption item 3 (peer-side): every node runs its own
model server and its own Open WebUI, and a node's own prompts never leave its
containers. The part that matters here is the boundary, not the existence of a
compose file: nothing in the stack may be reachable from the overlay or from a
neighbouring node, and nothing in it may carry a signing or cluster capability.
The `sign` capability is the one the peer roles deliberately withhold; a key or
a signer socket mounted here would be the same failure the admin surface
(`master-node/tests/test_admin_surface.py`) guards against, in the
confidentiality direction.

These tests read the manifest and the compose file — they prove the *declared*
topology is coherent, exactly as the admin surface's do, and no stronger. The
deployed healthcheck (pull the images on a target node, run the stack, confirm
both surfaces answer with no GPU present) is `DoD: confirm: user` and is
recorded rather than claimed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROLE_ROOT = Path(__file__).resolve().parents[1]
FABRIC_ROOT = ROLE_ROOT.parent
MANIFEST = ROLE_ROOT / "manifests" / "ai-stack.json"
STACK_DIR = ROLE_ROOT / "configs" / "ai-stack"
COMPOSE = STACK_DIR / "docker-compose.yml"

#: Tokens that must never appear in the compose file outside a comment. Each is
#: the fingerprint of a structural crossing the stack must not make: key material,
#: the signer, the state dir that holds them, or the overlay address any peer
#: service uses.
FORBIDDEN = (
    "signer.sock",
    "SPARKNET_STATE_DIR",
    "operational.ed25519",
    "master-ed25519",
    "10.100.0.",
    "/keys",
    ".state/",
    "OLLAMA_BASE_URL: http://10",  # a remote / overlay model server, not this node's
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _compose_services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


# --- the topology is the two own-inference services, and nothing more ----------


def test_the_stack_declares_a_nodes_own_inference_services_only():
    services = _compose_services()
    assert set(services) == {"ollama", "open-webui"}, (
        "the AI stack is exactly the node's model server and its own chat surface; "
        f"anything else got {sorted(services)}"
    )


def test_the_manifest_and_compose_agree_on_the_service_set():
    manifest = _load(MANIFEST)
    assert set(_compose_services()) == set(manifest["topology"]["services"]), (
        "the manifest and the compose file must name the same services, or one of "
        "them is describing a stack the other does not ship"
    )


# --- every port binds to loopback, never the overlay or all interfaces ---------


def test_every_port_binds_to_loopback_only():
    for name, service in _compose_services().items():
        ports = service.get("ports", [])
        assert ports, f"{name} declares no ports; the stack is unreachable as specified"
        for entry in ports:
            assert entry.startswith("127.0.0.1:"), (
                f"{name} binds {entry!r}; only loopback is allowed. A bare ':port' "
                "binds every interface and an overlay address exposes the node's "
                "knowledge to the cluster, both of which invert this design."
            )
            assert "0.0.0.0" not in entry
            assert "10.100.0." not in entry


def test_the_stack_opens_no_overlay_or_admin_port():
    ports: set[str] = set()
    for service in _compose_services().values():
        for entry in service.get("ports", []):
            host_port = entry.split(":")[1]
            ports.add(host_port)
    assert "4223" not in ports, "4223 is the Master's shared vector index; a node must not claim it"
    assert "4210" not in ports, "4210 is the election exchange port; a node must not claim it"


# --- Open WebUI answers with THIS node's own model, and knowledge only from vault


def test_open_webui_uses_this_nodes_own_ollama():
    webui = _compose_services()["open-webui"]
    base = webui.get("environment", {}).get("OLLAMA_BASE_URL")
    assert base == "http://ollama:11434", (
        f"Open WebUI points at {base!r}; it must point at the compose service "
        "'ollama:11434', this node's own model server. An overlay or remote URL "
        "means the node stopped being a node and became a thin client."
    )


def test_open_webui_rag_is_disabled():
    webui = _compose_services()["open-webui"]
    assert webui.get("environment", {}).get("RAG_INGESTION_ENABLED") == "false", (
        "Open WebUI's own vector store would be a second, grant-less knowledge "
        "base; the vault is the only one (same rule as the Master admin surface)"
    )


# --- Step 5: the node's agents as the Open WebUI surface ----------------------


def test_open_webui_declares_the_node_agent_surface():
    webui = _compose_services()["open-webui"]
    env = webui.get("environment", {})
    assert env.get("OPENAI_API_BASE_URLS") == "http://host.docker.internal:4096/v1", (
        "the Step 5 surface is the node's own dispatch bridge (loopback 4096); "
        "any other base URL routes the node's agents through something foreign"
    )
    assert env.get("MODEL_FILTER_ENABLED") == "true"
    assert env.get("MODEL_FILTER_LIST") == "plan,build,vault,node-orchestrator,masterspike"
    assert env.get("DEFAULT_MODELS") == "plan"
    assert isinstance(webui.get("extra_hosts"), list) and any(
        line.startswith("host.docker.internal:host-gateway") for line in webui["extra_hosts"]
    ), "host.docker.internal must resolve to this host so the webui can reach the node's own bridge"


def test_the_bridge_key_is_a_deploy_time_variable_never_a_committed_token():
    webui = _compose_services()["open-webui"]
    key = webui.get("environment", {}).get("OPENAI_API_KEYS", "")
    assert key.startswith("${SPARKNET_WEBUI_BRIDGE_GRANT:"), key
    assert "do not commit" in key
    assert "sk-" not in key, "a standing bearer token must never be committed"


def test_the_step5_models_are_declared_in_agent_models_json():
    manifest = json.loads((ROLE_ROOT / "manifests" / "agent-models.json").read_text())
    assert manifest.get("schema") == "sparknet-agent-models/1"
    ids = [m["id"] for m in manifest["selectable"]]
    assert ids == ["plan", "build", "vault", "node-orchestrator", "masterspike"]
    assert all(m.get("agent") for m in manifest["selectable"])


def test_the_declared_models_match_the_webui_model_filter():
    manifest = json.loads((ROLE_ROOT / "manifests" / "agent-models.json").read_text())
    webui = _compose_services()["open-webui"]
    filter_list = webui.get("environment", {}).get("MODEL_FILTER_LIST", "").split(",")
    ids = [m["id"] for m in manifest["selectable"]]
    assert filter_list == ids, "the model filter and agent-models.json must name the same surface"


# --- the boundary the stack must not cross ------------------------------------


def test_the_compose_file_crosses_no_signing_or_state_boundary():
    """Strips comments, so a nod to the boundary in prose is not claimed as one."""
    text = "\n".join(line for line in COMPOSE.read_text().splitlines() if not line.lstrip().startswith("#"))
    for token in FORBIDDEN:
        assert token not in text, (
            f"the ai-stack compose references {token!r} outside a comment. The "
            "stack must never be the surface that reaches key material, the "
            "signer, the state dir, or another node on the overlay."
        )


def test_no_gpu_is_declared_in_the_committed_compose():
    text = "\n".join(line for line in COMPOSE.read_text().splitlines() if not line.lstrip().startswith("#"))
    for token in ("gpus:", "gpu:", "runtime: nvidia", "NVIDIA", "device_reservations"):
        assert token not in text, (
            f"the committed compose declares {token!r}; GPU is a per-node deploy "
            "decision and must not make the stack non-portable"
        )


def test_the_manifest_grants_no_authority_capability():
    caps = _load(MANIFEST)["capabilities"]
    assert caps["granted_to_this_stack"] == [], caps["granted_to_this_stack"]
    for withheld in ("sign", "cluster-dispatch", "cluster-serve"):
        assert withheld in caps["explicitly_not_granted"], (
            f"{withheld} must be named as explicitly not granted, or a future edit "
            "could add it without failing anything"
        )


def test_no_named_volume_is_made_of_the_nodes_state():
    """Model weights are server state and belong in a named volume; the node's
    vault `.state` (keys, authz) is the one thing that must never get here."""
    volumes = yaml.safe_load(COMPOSE.read_text()).get("volumes", {})
    assert list(volumes) == ["ollama-models"], f"unexpected stack volumes: {list(volumes)}"
    assert ".state" not in volumes, "the node vault's .state must not be a stack volume"


# --- the compose parses, when this host has docker ---------------------------


def test_compose_config_parses_when_docker_is_available(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH; the file-reading assertions above still apply")
    env = dict(__import__("os").environ)
    env["SPARKNET_WEBUI_BRIDGE_GRANT"] = "placeholder-grant"
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
        cwd=ROLE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"docker compose rejects the ai-stack file:\n{result.stdout}\n{result.stderr}"
