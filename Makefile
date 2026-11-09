# sparknet peer-node — developer entry points
#
# Standalone: every target works from this directory alone. The parent's
# contracts/ and shared/ are located via SPARKNET_FABRIC_ROOT (see
# scripts/sparknet-env.sh), overridable for a deployment that stages them
# elsewhere.

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help
VAULT_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
ROLE_ROOT := $(VAULT_ROOT)
FABRIC_ROOT := $(patsubst %/,%,$(dir $(VAULT_ROOT)))

# Interpreter: this subvault's optional .venv if it exists, else the fabric
# venv `make devtools` creates at the fabric root, else the system one. The
# middle tier is what a fabric checkout provisions — `make ci` never creates a
# role venv, so without it every role target here falls through to whatever
# python is installed, which on a runner has no `cryptography`.
VENV_PY := $(VAULT_ROOT)/.venv/bin/python
FABRIC_VENV_PY := $(FABRIC_ROOT)/.venv/bin/python
PY := $(if $(wildcard $(VENV_PY)),$(VENV_PY),$(if $(wildcard $(FABRIC_VENV_PY)),$(FABRIC_VENV_PY),python3))

.PHONY: help env-init validate test lint devtools provision dist bundle install-units \
        install-config keys wg-render authz-verify anchor guard-status guard-contract \
        peer-manifest containment wiki-preflight wiki-status wiki-lint wiki-capture skills-sync skills-check clean \
        provenance-verify user-init provenance-append ingest accept-inbox review-inbox precommit shims-check keys-check context-pack context-check governance-check exec-policy-check exec-daemon \
        ai-stack-config admin-agent-help container-build container-load container-config container-preflight container-up \
        container-health container-down model-generate model-verify model-build model-serve rfi-request vault-health vault-heal \
        opencode-runtime opencode-runtime-clean opencode-certify

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env-init: ## Write this role's .env + wiki dirs from .env.example (idempotent)
	@$(FABRIC_ROOT)/scripts/sparknet-env-init.sh "$(VAULT_ROOT)"

context-pack: ## Regenerate this role's CONTEXT.md
	@$(PY) $(FABRIC_ROOT)/scripts/sparknet-context-pack.py --root "$(VAULT_ROOT)" --role peer

governance-check: ## Enforce the five-file shape and budgets
	@$(PY) $(FABRIC_ROOT)/scripts/sparknet-governance-check.py

context-check: ## Fail if this role's CONTEXT.md is stale
	@$(PY) $(FABRIC_ROOT)/scripts/sparknet-context-pack.py --root "$(VAULT_ROOT)" --role peer --check

validate: ## Env contract + manifest checks
	@./scripts/sparknet-validate.sh

test: ## Run this role's pytest suite
	@$(PY) -m pytest

lint: ## Ruff if available; otherwise explains how to get it
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check . && ruff format --check . ; \
	elif [ -x "$(VAULT_ROOT)/.venv/bin/ruff" ]; then \
		"$(VAULT_ROOT)/.venv/bin/ruff" check . && "$(VAULT_ROOT)/.venv/bin/ruff" format --check . ; \
	else \
		echo "ruff not installed — cannot lint."; \
		echo "Run 'make devtools' at the fabric root, or use 'make lint' there,"; \
		echo "which lints this role with the fabric interpreter."; \
		echo "Not skipping silently: a lint gate that cannot run is not a gate."; \
		exit 1; \
	fi

devtools: ## Create .venv with cryptography + pytest + ruff (opt-in)
	@# venv's argument is the DIRECTORY, not the interpreter inside it; passing
	@# $(VENV_PY) built a venv under .venv/bin/python/ and broke every later
	@# exec of $(VENV_PY) on a clone that did not already have one.
	@if [ -x "$(VENV_PY)" ]; then \
		echo "$(VENV_PY) already exists; refreshing deps"; \
	else \
		echo "Creating $(VAULT_ROOT)/.venv"; \
		python3 -m venv "$(VAULT_ROOT)/.venv"; \
	fi
	@"$(VENV_PY)" -m pip install --quiet --upgrade pip
	@# This subvault is not a distributable package, so install dependencies by
	@# name rather than `pip install -e .` (which would need a build backend and
	@# would try to package the vault itself).
	@"$(VENV_PY)" -m pip install --quiet -r "$(VAULT_ROOT)/requirements.txt"
	@"$(VENV_PY)" -m pip install --quiet -r "$(VAULT_ROOT)/requirements-dev.txt"
	@"$(VENV_PY)" -m pytest --version
	@"$(VENV_PY)" -m ruff --version
	@echo "Dev tools ready. make lint and make test will now use .venv."

provision: ## Identity capture, then validate
	@./scripts/sparknet-identity-capture.sh
	@./scripts/sparknet-validate.sh

# A peer-node/ archive on its own is NOT a deployment: validation fails without
# contracts/ and shared/, which is exactly the point — a peer cannot verify a
# signature against a contract it does not have, so it refuses to pretend to.
# `make dist` therefore builds the real bundle via the parent script.
#
#   make dist                    build from the working tree (warns if dirty)
#   make dist FROM_GIT=<ref>     build from a commit (release-grade)
#   make dist VERIFY=1           extract and prove it runs standalone
dist: ## Build a standalone peer bundle into ../dist
	@$(FABRIC_ROOT)/scripts/sparknet-bundle.sh peer $(if $(FROM_GIT),--from-git $(FROM_GIT),)
	@if [ -n "$(VERIFY)" ]; then \
		$(FABRIC_ROOT)/scripts/sparknet-bundle.sh --verify \
			$$(ls -t "$(FABRIC_ROOT)"/dist/sparknet-peer-*.tar.gz | head -1); \
	fi

exec-policy-check: ## Validate the execution allowlist (no execution)
	@$(PY) -c "import json; from pathlib import Path; d=json.loads(Path('$(VAULT_ROOT)/configs/exec-policy.json').read_text()); assert d['schema']=='sparknet-exec-policy/1'; print('exec policy: valid')"

exec-daemon: ## Run the argv-only daemon in validate-only mode (foreground)
	@$(PY) "$(FABRIC_ROOT)/scripts/sparknet-exec-daemon.py" --socket "$${SPARKNET_EXEC_SOCKET:-/tmp/sparknet-exec.sock}" --vault-root "$(VAULT_ROOT)" --node-pub "$(VAULT_ROOT)/.state/keys/ed25519-node.pub.b64" --signing-key "$(VAULT_ROOT)/.state/keys/ed25519-node.key.b64" --policy "$(VAULT_ROOT)/configs/exec-policy.json"

install-units: ## Generate shims + install systemd units (dry-run; pass APPLY=1 to write)
	@./scripts/sparknet-install-units.sh $(if $(APPLY),--apply,)

install-config: ## Show the /etc config diff. Never writes, by design.
	@./scripts/sparknet-install-config.sh

# --- Master-anchored node operations ----------------------------------------

KEY_OUT ?= $(VAULT_ROOT)/.state/keys

keys: ## Generate WireGuard + Ed25519 keypairs into $(KEY_OUT) (secrets, never committed)
	@SPARKNET_KEY_DIR="$(KEY_OUT)" ./scripts/sparknet-keygen.sh --role node

wg-render: ## Render <iface>.conf from the template (default wg0; SPARKNET_WG_INTERFACE overrides). Dry-run; APPLY=1 writes at 0600
	@./scripts/sparknet-wireguard-render.sh $(if $(APPLY),--apply,)

authz-verify: ## Verify this node's identity against the signed registry
	@./scripts/sparknet-run $(PY) "$(FABRIC_ROOT)/shared/sparknet-authz.py" verify \
		"$(if $(IDENTITY),$(IDENTITY),manifests/node-identity.json)" \
		--registry "$(if $(REGISTRY),$(REGISTRY),manifests/node-authorizations.json)" \
		--master-pubkey "$(if $(MASTER_PUBKEY),$(MASTER_PUBKEY),manifests/keys/master-ed25519.pub.b64)"

anchor: ## Bring up the WireGuard Master Anchor (root; render + wg-quick up + chattr +i)
	@./scripts/sparknet-wireguard-anchor.sh

containment: ## THIS NODE's root-gated teardown. Not the cluster KillSwitch (Master-side).
	@echo "This tears down only this node's anchor. The cluster KillSwitch is a Master"
	@echo "action (master-node/services/sparknet-killswitch.py) and this role has no"
	@echo "copy or target for it. Continuing with node-local containment in 3s (Ctrl-C to abort)."
	@sleep 3
	@./scripts/sparknet-node-containment.sh $(if $(DRY_RUN),--dry-run,)

guard-status: ## Report the guard's manifest state and authorization posture
	@./scripts/sparknet-run $(PY) ./services/sparknet-guard-d.py --check-manifest

# --- peer-role contract conformance -----------------------------------------
# These read the shared contract from the parent and prove this role still
# agrees with it. They are the mechanism that stops the two roles drifting.

# The contract tests live at the parent, not here: they exist to catch the two
# roles disagreeing, which is only observable from outside either one. A copy
# here could be edited to pass and would then be the thing under test.
guard-contract: ## Prove this role matches ../contracts (capabilities, ports, overlay)
	@"$(FABRIC_ROOT)/.venv/bin/python" -m pytest \
		"$(FABRIC_ROOT)/tests/test_contracts.py" -q \
		|| "$(PY)" -m pytest "$(FABRIC_ROOT)/tests/test_contracts.py" -q

peer-manifest: ## Print this node's identity and authorization posture
	@$(PY) -c "import json,pathlib; \
d=json.loads(pathlib.Path('manifests/node-identity.json').read_text()); \
print('node_id:  ', d['node_id']); \
print('role:     ', d['role']); \
print('overlay:  ', d['overlay_ipv4']); \
print('requests: ', ', '.join(d['requested_capabilities']))"

# --- provenance (the numbered layout's trust layer) ---------------------------
#
# Everything here delegates to ../shared/sparknet-provenance.py: one
# implementation, loaded from the fabric, never copied into this subvault.

PROV := $(FABRIC_ROOT)/shared/sparknet-provenance.py
PROV_PY := $(if $(wildcard $(FABRIC_ROOT)/.venv/bin/python),$(FABRIC_ROOT)/.venv/bin/python,$(PY))
PROV_VAULT := --vault "$(VAULT_ROOT)"

provenance-verify: ## Verify every note, the ledger and the approvals (STRICT=1 for --strict)
	@$(PROV_PY) $(PROV) $(PROV_VAULT) verify $(if $(STRICT),--strict,)

user-init: ## Generate the user key + fill 00_System/user.md once (FORCE=1 to redo)
	@$(PROV_PY) $(PROV) $(PROV_VAULT) user-init $(if $(FORCE),--force,) \
		$(if $(OWNER_ID),--owner-id "$(OWNER_ID)",)

provenance-append: ## Record a dual-signed approval (QUESTION= DECISION= AGENT= MODEL= [NOTE=...])
	@test -n "$(QUESTION)" || { echo "QUESTION= required" >&2; exit 1; }
	@test -n "$(DECISION)" || { echo "DECISION= required" >&2; exit 1; }
	@test -n "$(AGENT)"     || { echo "AGENT= required"     >&2; exit 1; }
	@test -n "$(MODEL)"     || { echo "MODEL= required"     >&2; exit 1; }
	@$(PROV_PY) $(PROV) $(PROV_VAULT) append-approval --question "$(QUESTION)" \
		--decision "$(DECISION)" --agent "$(AGENT)" --model "$(MODEL)" \
		$(if $(NOTE),--note $(NOTE),)

ingest: ## Land STDIN or SRC= as an unsigned note in 01_Inbox/ (SOURCE=master-node|peer-node|email)
	@$(PROV_PY) $(PROV) $(PROV_VAULT) ingest $(if $(SRC),"$(SRC)","-") \
		--source "$(if $(SOURCE),$(SOURCE),peer-node)" \
		$(if $(SENDER),--sender "$(SENDER)",)

accept-inbox: ## Record an inbox acceptance (ITEM=01_Inbox/... SOURCE=... [REVIEWER=... SANITIZED=1])
	@test -n "$(ITEM)"   || { echo "ITEM=01_Inbox/<item>.md required" >&2; exit 1; }
	@test -n "$(SOURCE)" || { echo "SOURCE=peer-node|email|master-node required" >&2; exit 1; }
	@$(PROV_PY) $(PROV) $(PROV_VAULT) accept-inbox "$(ITEM)" --source "$(SOURCE)" \
		--decision "$(if $(DECISION),$(DECISION),accepted)" \
		$(if $(REVIEWER),--reviewer "$(REVIEWER)",) \
		$(if $(SANITIZED),--sanitized,) $(if $(SENDER),--sender "$(SENDER)",) \
		$(if $(NOTE),--note $(NOTE),)

review-inbox: ## Sanitize an accepted untrusted item (ITEM=... [APPLY=1] [ALLOW_INSTRUCTIONS=1])
	@test -n "$(ITEM)" || { echo "ITEM=01_Inbox/<item>.md required" >&2; exit 1; }
	@$(PY) "$(VAULT_ROOT)/agents/vault-curator/agent.py" review --vault "$(VAULT_ROOT)" "$(ITEM)" \
		$(if $(APPLY),--apply,) $(if $(ALLOW_INSTRUCTIONS),--allow-instructions,)

shims-check: ## Prove every legacy name is a symlink to its numbered home
	@$(PROV_PY) $(PROV) $(PROV_VAULT) shims

keys-check: ## No key material tracked; state-dir keys are 0600
	@$(FABRIC_ROOT)/scripts/sparknet-keys-check.sh "$(VAULT_ROOT)"

precommit: keys-check skills-check ## keys-check + skill manifest + secret scan + provenance-verify --strict
	@$(PROV_PY) $(PROV) $(PROV_VAULT) scan
	@$(PROV_PY) $(PROV) $(PROV_VAULT) verify --strict
	@echo "precommit: clean"

# --- wiki skill suite (vendored in the repo, no global skill farm) -------------
#
# Every target sources the preflight first: the vendored skills resolve their
# vault by walking up from $PWD for a .env, so without this check a missing role
# .env would quietly point the whole session at the parent fabric vault.

wiki-preflight: ## Prove the vendored wiki skills will resolve THIS role's vault
	@source "$(FABRIC_ROOT)/scripts/sparknet-wiki-env.sh"

wiki-status: ## Preflight, then /wiki-status on this role's vault
	@source "$(FABRIC_ROOT)/scripts/sparknet-wiki-env.sh"
	@echo "Run /wiki-status in opencode to see the ingest delta for this role."

wiki-lint: ## Preflight, then /wiki-lint on this role's vault
	@source "$(FABRIC_ROOT)/scripts/sparknet-wiki-env.sh"
	@echo "Run /wiki-lint in opencode to audit orphans, stale pages and broken links."

wiki-capture: ## Preflight, then /wiki-capture (--quick for a raw drop)
	@source "$(FABRIC_ROOT)/scripts/sparknet-wiki-env.sh"
	@echo "Run /wiki-capture in opencode to persist a finding from this session."

skills-sync: ## Rebuild this role's local skill catalog
	@$(PY) $(FABRIC_ROOT)/scripts/sync-role-skills.py --apply --role peer

skills-check: ## Verify this role's skill catalog, licenses and manifest
	@$(PY) $(FABRIC_ROOT)/scripts/sync-role-skills.py --check --role peer

clean: ## Remove caches and build output
	@find "$(VAULT_ROOT)" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf "$(VAULT_ROOT)/.pytest_cache" "$(VAULT_ROOT)/dist"
	@echo "Cleaned caches and dist/ (left .venv, .state and .cache alone)."
# --- peer registry/wg helpers ---
.PHONY: registry-fetch registry-status registry-verify-peer wg-render-peer

# The live channel is the Master pushing a signed publication to the guard on
# 9090. This target is the offline path: install one the Master delivered, or an
# operator carried to a node whose guard is not running yet. Same acceptance
# rule as the guard (shared/sparknet-registry-install.py).
registry-fetch: ## install a signed registry publication (PUBLICATION=... [DRY_RUN=1])
	@test -n "$(PUBLICATION)" || (echo "PUBLICATION=<publication.json> required" >&2; exit 1)
	@$(ROLE_ROOT)/scripts/sparknet-registry-fetch.sh --publication "$(PUBLICATION)" \
	  $(if $(DRY_RUN),--dry-run) $(if $(JSON),--json)

vault-health: ## Diagnose the vault (read-only; writes 00_System/vault-health.json)
	@$(PY) $(FABRIC_ROOT)/scripts/sparknet-vault-health.py --root "$(VAULT_ROOT)"

vault-heal: ## Safe derived repairs only (skeleton, shims, CONTEXT.md, health report)
	@$(PY) $(FABRIC_ROOT)/scripts/sparknet-vault-health.py --root "$(VAULT_ROOT)" --repair

rfi-request: ## Ask the Master for scoped knowledge (QUESTION= SCOPE=cat1,cat2 [REQUEST_ID=])
	@test -n "$(QUESTION)" || (echo "QUESTION=... is required" >&2; exit 1)
	@test -n "$(SCOPE)" || (echo "SCOPE=cat1,cat2 is required" >&2; exit 1)
	@$(PY) "$(VAULT_ROOT)/services/sparknet-rfi-client.py" --vault-root "$(VAULT_ROOT)" \
	  --question "$(QUESTION)" --scope "$(SCOPE)" $(if $(REQUEST_ID),--request-id "$(REQUEST_ID)") \
	  $(if $(ENDPOINT),--endpoint "$(ENDPOINT)")

registry-status: ## installed publication sequence and this node's capabilities
	@$(ROLE_ROOT)/scripts/sparknet-registry-fetch.sh --status

registry-verify-peer: ## verify registry (needs REGISTRY)
	@test -n "$(REGISTRY)" || (echo "REGISTRY=<registry.json> required" >&2; exit 1)
	@$(ROLE_ROOT)/scripts/sparknet-registry-verify-peer.sh --registry "$(REGISTRY)"

wg-render-peer: ## render peer wg (needs REGISTRY)
	@test -n "$(REGISTRY)" || (echo "REGISTRY=<registry.json> required" >&2; exit 1)
	@$(ROLE_ROOT)/scripts/sparknet-wg-render-peer.sh --registry "$(REGISTRY)" $(if $(OUT),--out $(OUT))


# --- election ---
.PHONY: election-request

# Runs the shared member ceremony (bid -> vote -> co-sign -> install).
# Other members endorse a requester with: CANDIDATE=<node-id> make election-request
election-request: ## run this node's part of the election (URL= CANDIDATE= NODE_ID= IDENTITY= KEY_DIR= WAIT=)
	@$(ROLE_ROOT)/scripts/sparknet-election-request.sh \
	  $(if $(URL),--url "$(URL)") \
	  $(if $(NODE_ID),--node-id "$(NODE_ID)") \
	  $(if $(CANDIDATE),--candidate "$(CANDIDATE)") \
	  $(if $(IDENTITY),--identity "$(IDENTITY)") \
	  $(if $(KEY_DIR),--key-dir "$(KEY_DIR)") \
	  $(if $(WAIT),--wait-seconds "$(WAIT)")

ai-stack-config: ## Validate the node AI stack compose parses (no images pulled)
	@docker compose -f "$(VAULT_ROOT)/configs/ai-stack/docker-compose.yml" config --quiet

admin-agent-help: ## Admin agent --help / --serve (peer PLAN Step 12; no-op without --serve)
	@$(PY) "$(VAULT_ROOT)/services/sparknet-admin-agent.py" --help

# --- node container (peer PLAN Step 2) ----------------------------------------
#
# `make dist` assembles the bundle first: what runs inside the image is exactly
# what ships in the bundle — one code path. The staged tarball is gitignored.
# OPENCODE_VERSION / CONTAINER_PLATFORMS are the knobs a rebuild changes; the
# image and compose must agree on OPENCODE_VERSION (the test suite asserts it).

OPENCODE_VERSION ?= 2.2.5
CONTAINER_PLATFORMS ?= linux/arm64,linux/amd64
# The buildkit sandbox DNS on the fabric host is unreliable (apt/git clone fail
# with "Temporary failure resolving"); joining the host network namespace makes
# the build deterministic. Harmless for the deployed path (a registry push).
CONTAINER_BUILD_NET ?= --network=host
# The local store cannot --load a manifest list; this maps uname to buildx.
HOST_ARCH := $(shell uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')

OPENCODE_RUNTIME_VERSION ?= 2.2.5
OPENCODE_RUNTIME_BASE ?= sparknet-spark-node:dev
OPENCODE_RUNTIME_DIR := $(VAULT_ROOT)/configs/opencode-runtime

opencode-runtime: ## Build the per-user OpenCode sandbox image (pinned, no CUDA)
	@set -e; arch="$$(uname -m)"; case "$$arch" in 	  aarch64|arm64) asset=arm64; targetarch=arm64 ;; 	  x86_64|amd64) asset=x64; targetarch=amd64 ;; 	  *) echo "unsupported host arch: $$arch" >&2; exit 1 ;; 	esac; 	tarball="$(OPENCODE_RUNTIME_DIR)/opencode-linux-$$targetarch.tar.gz"; 	if [ ! -s "$$tarball" ]; then 	  echo "downloading opencode $(OPENCODE_RUNTIME_VERSION) ($$asset)"; 	  curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v$(OPENCODE_RUNTIME_VERSION)/opencode-linux-$$asset.tar.gz" -o "$$tarball"; 	fi; 	docker build 	  --build-arg BASE_IMAGE="$(OPENCODE_RUNTIME_BASE)" 	  --build-arg OPENCODE_VERSION="$(OPENCODE_RUNTIME_VERSION)" 	  -t "sparknet-opencode-runtime:$(OPENCODE_RUNTIME_VERSION)" 	  "$(OPENCODE_RUNTIME_DIR)"; 	docker run --rm --entrypoint opencode "sparknet-opencode-runtime:$(OPENCODE_RUNTIME_VERSION)" --version

opencode-runtime-clean: ## Remove the downloaded sandbox runtime tarball
	@rm -f "$(OPENCODE_RUNTIME_DIR)"/opencode-linux-*.tar.gz

opencode-certify: ## Run the real pinned OpenCode v2 API certification
	@set -e; arch="$$(uname -m)"; case "$$arch" in 	  aarch64|arm64) asset=arm64 ;; 	  x86_64|amd64) asset=x64 ;; 	  *) echo "unsupported host arch: $$arch" >&2; exit 1 ;; 	esac; 	tmp="$$(mktemp -d)"; trap 'rm -rf "$$tmp"' EXIT; 	echo "downloading opencode $(OPENCODE_RUNTIME_VERSION) ($$asset) for certification"; 	curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v$(OPENCODE_RUNTIME_VERSION)/opencode-linux-$$asset.tar.gz" -o "$$tmp/opencode.tar.gz"; 	tar -xzf "$$tmp/opencode.tar.gz" -C "$$tmp" opencode; 	OPENCODE_BIN="$$tmp/opencode" "$(PY)" -m pytest "$(FABRIC_ROOT)/tests/test_opencode_v2_integration.py" -q

container-build: ## make dist, stage the bundle, buildx both architectures (registry-ready)
	@$(MAKE) --no-print-directory dist $(if $(FROM_GIT),FROM_GIT=$(FROM_GIT),)
	@mkdir -p "$(VAULT_ROOT)/container/context"
	@cp "$$(ls -t "$(FABRIC_ROOT)"/dist/sparknet-peer-*.tar.gz | head -1)" \
		"$(VAULT_ROOT)/container/context/sparknet-peer.tar.gz"
	@docker buildx build $(CONTAINER_BUILD_NET) --platform "$(CONTAINER_PLATFORMS)" \
		--build-arg OPENCODE_VERSION="$(OPENCODE_VERSION)" \
		-t "sparknet-peer-node:$(OPENCODE_VERSION)" \
		-f "$(VAULT_ROOT)/container/Dockerfile" "$(FABRIC_ROOT)"
	@docker buildx build $(CONTAINER_BUILD_NET) --platform "$(CONTAINER_PLATFORMS)" \
		-t "sparknet-node-anchor:0.0.20230223" \
		-f "$(VAULT_ROOT)/container/anchor/Dockerfile" "$(VAULT_ROOT)/container"

container-load: ## Build the HOST architecture with --load, for local runs/tests
	@docker buildx build $(CONTAINER_BUILD_NET) --load --platform "linux/$(HOST_ARCH)" \
		--build-arg OPENCODE_VERSION="$(OPENCODE_VERSION)" \
		-t "sparknet-peer-node:$(OPENCODE_VERSION)" \
		-f "$(VAULT_ROOT)/container/Dockerfile" "$(FABRIC_ROOT)"
	@docker buildx build $(CONTAINER_BUILD_NET) --load --platform "linux/$(HOST_ARCH)" \
		-t "sparknet-node-anchor:0.0.20230223" \
		-f "$(VAULT_ROOT)/container/anchor/Dockerfile" "$(VAULT_ROOT)/container"

container-config: ## Validate the node container compose parses (no images pulled)
	@OPENCODE_VERSION="$(OPENCODE_VERSION)" docker compose -f "$(VAULT_ROOT)/configs/container/docker-compose.yml" config --quiet

container-preflight: ## Prove chattr +i holds on the .state filesystem before relying on it
	@./scripts/sparknet-chattr-preflight.sh

container-up: container-preflight ## Preflight, then bring the node container up
	@OPENCODE_VERSION="$(OPENCODE_VERSION)" docker compose -f "$(VAULT_ROOT)/configs/container/docker-compose.yml" up -d

container-health: ## Per-service container health
	@OPENCODE_VERSION="$(OPENCODE_VERSION)" docker compose -f "$(VAULT_ROOT)/configs/container/docker-compose.yml" ps -a

container-down: ## Tear the node container down
	@OPENCODE_VERSION="$(OPENCODE_VERSION)" docker compose -f "$(VAULT_ROOT)/configs/container/docker-compose.yml" down

# --- Step 7 MASTERSPIKE: the llm-wiki format as an inference artifact ----------
# Both the Ollama Modelfile and the vLLM system-prompt artifact are generated
# from .opencode/skills/llm-wiki/SKILL.md (scripts/sparknet-masterspike.py), so
# switching inference backends cannot silently drop the knowledge format. The
# model carries a format, never a capability: authority stays Ed25519 in the
# Master. model-build/serve need a real Ollama daemon; model-verify does not.

.PHONY: model-generate model-verify model-build model-serve

model-generate: ## Regenerate the MASTERSPIKE artifacts from the llm-wiki skill
	@$(PY) scripts/sparknet-masterspike.py generate

model-verify: ## Fail if the MASTERSPIKE artifacts diverge from the llm-wiki skill
	@$(PY) scripts/sparknet-masterspike.py verify

model-build: ## Create the Ollama MASTERSPIKE model (requires a running ollama server)
	@ollama create sparknet-masterspike -f "manifests/masterspike/Modelfile"

model-serve: ## Serve MASTERSPIKE through a running Ollama daemon
	@ollama serve
