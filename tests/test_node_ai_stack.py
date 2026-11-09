"""The node's own AI stack — declared, and coherent as declared.

CLUSTER_INFERENCE_DESIGN.md adoption item 3 (peer-side): every node runs its own
chat surface on its own model server and its own vector store, and a node's own
prompts never leave its containers. The part that matters here is the boundary,
not the existence of a compose file: nothing in the stack may carry a signing or
cluster capability, and its one published port is paired with a host firewall
rule. The `sign` capability is the one the peer roles deliberately withhold; a key
or a signer socket mounted here would be the same failure the admin surface
(`master-node/tests/test_admin_surface.py`) guards against, in the
confidentiality direction.

Operator decision (2026-10-10): the chat surface is published on `0.0.0.0:8181`
for LAN + overlay reachability, and a `SPARKNET-FW` host firewall keeps those
ports off the public internet. These tests read the manifest and the compose file
— they prove the *declared* topology is coherent, exactly as the admin surface's
do, and no stronger. The deployed healthcheck (pull the image on a target node,
run the stack, confirm the surface answers, create the MASTERSPIKE model, verify
the firewall) is `DoD: confirm: user` and is recorded rather than claimed.
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


# --- the topology is the one own-inference surface, and nothing more ----------


def test_the_stack_declares_the_nodes_own_chat_surface_only():
    services = _compose_services()
    assert set(services) == {"open-webui", "nginx"}, (
        f"the stack is Open WebUI plus the same-origin nginx proxy; anything else got {sorted(services)}"
    )


def test_the_manifest_and_compose_agree_on_the_service_set():
    manifest = _load(MANIFEST)
    assert set(_compose_services()) == set(manifest["topology"]["services"]), (
        "the manifest and the compose file must name the same services, or one of "
        "them is describing a stack the other does not ship"
    )


# --- the one published port is the firewalled chat surface --------------------


def test_the_only_published_port_is_the_firewalled_webui():
    webui = _compose_services()["open-webui"]
    assert webui.get("network_mode") == "host", (
        "Open WebUI must share the host netns so it can reach the node's "
        "loopback-only dispatch bridge (127.0.0.1:4096); a bridge-network "
        "container cannot reach a loopback-only host listener"
    )
    assert webui.get("ports") is None, "with network_mode: host the surface binds through PORT, not a ports map"
    assert webui.get("environment", {}).get("HOST") == "127.0.0.1"
    assert webui.get("environment", {}).get("PORT") == "8180"
    nginx = _compose_services()["nginx"]
    assert nginx.get("network_mode") == "host"
    assert nginx.get("environment", {}).get("SPARKNET_BIND") == "8181"
    assert any("nginx.conf.template" in str(volume) for volume in nginx.get("volumes", []))
    assert any("sparknet-menu.js" in str(volume) for volume in nginx.get("volumes", []))
    lookup = _load(MANIFEST)["topology"]
    assert lookup["published_ports"] == ["0.0.0.0:8181 (host netns)"]
    assert "SPARKNET-FW" in json.dumps(lookup) or "firewall" in json.dumps(_load(MANIFEST)["network_boundary"])


def test_the_stack_opens_no_overlay_or_admin_port():
    ports: set[str] = set()
    for service in _compose_services().values():
        for entry in service.get("ports", []):
            host_port = entry.split(":")[1]
            ports.add(host_port)
    assert "4223" not in ports, "4223 is the Master's shared vector index; a node must not claim it"
    assert "4210" not in ports, "4210 is the election exchange port; a node must not claim it"


# --- Open WebUI answers with THIS node's own model, knowledge from the node ---


def test_open_webui_uses_this_nodes_own_ollama():
    webui = _compose_services()["open-webui"]
    base = webui.get("environment", {}).get("OLLAMA_BASE_URL")
    assert base == "http://127.0.0.1:11434", (
        f"Open WebUI points at {base!r}; it must point at this node's own infra "
        "Ollama on the host loopback (network_mode: host). An overlay or remote "
        "URL means the node stopped being a node and became a thin client."
    )


def test_open_webui_rag_is_qdrant_backed_and_structure_scoped():
    webui = _compose_services()["open-webui"]
    env = webui.get("environment", {})
    assert env.get("RAG_INGESTION_ENABLED") == "true", (
        "the operator enabled Open WebUI RAG (2026-10-10), now backed by the node's own Qdrant"
    )
    assert env.get("VECTOR_DB") == "qdrant"
    assert env.get("QDRANT_URI") == "http://127.0.0.1:6333"
    assert "CHROMA_HTTP_HOST" not in env, "the Chroma transport must not survive the migration"
    manifest = _load(MANIFEST)["knowledge_bases"]["open_webui_rag"]
    assert manifest["corpus"].startswith("structure-only"), manifest["corpus"]
    assert "00_System/user.md" in manifest["excluded"], "personal pages must be excluded from the corpus"


def test_qdrant_compose_is_loopback_only_and_keyed():
    qdrant_compose = STACK_DIR / "docker-compose.qdrant.yml"
    services = yaml.safe_load(qdrant_compose.read_text())["services"]
    assert set(services) == {"qdrant"}
    qdrant = services["qdrant"]
    ports = qdrant.get("ports", [])
    assert ports == ["127.0.0.1:6333:6333", "127.0.0.1:6334:6334"], (
        "Qdrant is a node-local store; exposing it on 0.0.0.0 or the overlay "
        f"would bypass the signed 4223 surface: {ports}"
    )
    env = qdrant.get("environment", {})
    key = env.get("QDRANT__SERVICE__API_KEY", "")
    assert key.startswith("${SPARKNET_QDRANT_API_KEY:"), key
    assert "do not commit" in key
    assert qdrant.get("volumes"), "the index must survive a container recreation"


# --- Step 5: the node's agents as the Open WebUI surface ----------------------


def test_open_webui_declares_the_opencode_engine_surface():
    webui = _compose_services()["open-webui"]
    env = webui.get("environment", {})
    assert env.get("OPENAI_API_BASE_URLS") == "http://127.0.0.1:4204/v1", (
        "the agent execution surface is the loopback OpenCode engine; the old "
        "dispatch bridge is the authority/vault surface, not a fake assistant"
    )
    assert env.get("MODEL_FILTER_ENABLED") == "true"
    filter_list = env.get("MODEL_FILTER_LIST", "").split(",")
    assert {"sparknet-chat", "sparknet-plan", "sparknet-build"} <= set(filter_list)
    assert any("masterspike" in item for item in filter_list)
    assert env.get("DEFAULT_MODELS") == "sparknet-chat", (
        "the default is the vault-aware engine chat; a local Ollama model stays selectable as a fallback"
    )
    assert webui.get("network_mode") == "host", (
        "host networking is what lets the webui reach the loopback engine; a "
        "bridge-network container cannot reach 127.0.0.1:4204"
    )


def test_the_engine_key_is_a_deploy_time_variable_never_a_committed_token():
    webui = _compose_services()["open-webui"]
    key = webui.get("environment", {}).get("OPENAI_API_KEYS", "")
    assert key.startswith("${SPARKNET_OPENCODE_ENGINE_TOKEN:"), key
    assert "do not commit" in key
    assert "sk-" not in key, "a standing bearer token must never be committed"


def test_the_first_admin_and_signup_are_deploy_time_settings_with_no_committed_secret():
    """Signup is enabled but approval-mediated; the node's admin is a machine account.

    ADMIN_FEDERATION_DESIGN.md: a peer's WebUI admin is a service account whose
    password is generated at deploy under the node state dir (0600), read only by
    the provisioner and the admin agent — so no human logs in as administrator on
    a peer. New accounts land `pending` and reach nothing until approved.
    """
    webui = _compose_services()["open-webui"]
    env = webui["environment"]
    assert env.get("ENABLE_SIGNUP") == "true", (
        "signup is enabled under the shared admin-federation design; the gate is "
        "DEFAULT_USER_ROLE=pending, not a disabled signup form"
    )
    assert env.get("DEFAULT_USER_ROLE") == "pending", (
        "new accounts must land pending until an admin accepts them — pending confers zero access"
    )
    assert "${SPARKNET_WEBUI_ADMIN_PASSWORD" in env.get("WEBUI_ADMIN_PASSWORD", ""), (
        "the machine-admin password must be a deploy-time variable, never a committed value"
    )
    assert "${SPARKNET_WEBUI_ADMIN_EMAIL" in env.get("WEBUI_ADMIN_EMAIL", "")


def test_the_admin_federation_surface_is_declared_in_the_manifest():
    manifest = _load(MANIFEST)
    fed = manifest["admin_federation"]
    assert "ADMIN_FEDERATION_DESIGN.md" in fed["design"]
    assert "pending" in fed["signup"]
    assert "service_account_admin" in fed
    assert "sparknet-admin-agent.py" in fed["admin_agent"]
    assert "admin-supervise" in fed["supervise"]
    webui = _compose_services()["open-webui"]
    assert webui["environment"].get("AUDIT_LOG_LEVEL") == "METADATA", (
        "the node's metadata audit log backs the event spool; both recorded"
    )


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
    local_ids = [m["id"] for m in manifest.get("local_models", [])]
    assert {"sparknet-chat", "sparknet-plan", "sparknet-build", "llama3.2:latest"} <= set(filter_list)
    assert any("masterspike" in item for item in filter_list)
    assert set(local_ids) <= set(filter_list), "the local student candidates stay selectable"


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
    """Chat/session state belongs in a named volume; the node's vault `.state`
    (keys, authz) is the one thing that must never get here."""
    volumes = yaml.safe_load(COMPOSE.read_text()).get("volumes", {})
    assert list(volumes) == ["open-webui-data"], f"unexpected stack volumes: {list(volumes)}"
    assert ".state" not in volumes, "the node vault's .state must not be a stack volume"


# --- the compose parses, when this host has docker ---------------------------


def test_compose_config_parses_when_docker_is_available(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not on PATH; the file-reading assertions above still apply")
    env = dict(__import__("os").environ)
    env["SPARKNET_OPENCODE_ENGINE_TOKEN"] = "placeholder-engine-token"
    env["SPARKNET_QDRANT_API_KEY"] = "placeholder-qdrant-key"
    for compose in (COMPOSE, STACK_DIR / "docker-compose.qdrant.yml"):
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose), "config", "--quiet"],
            cwd=ROLE_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, f"docker compose rejects {compose.name}:\n{result.stdout}\n{result.stderr}"
