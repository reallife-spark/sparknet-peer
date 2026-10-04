# sparknet peer-node — developer entry points
#
# Standalone: every target works from this directory alone. The parent's
# contracts/ and shared/ are located via SPARKNET_FABRIC_ROOT (see
# scripts/sparknet-env.sh), overridable for a deployment that stages them
# elsewhere.

SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help
VAULT_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
FABRIC_ROOT := $(patsubst %/,%,$(dir $(VAULT_ROOT)))

# Prefer the optional .venv when it exists, otherwise the system interpreter.
VENV_PY := $(VAULT_ROOT)/.venv/bin/python
PY := $(if $(wildcard $(VENV_PY)),$(VENV_PY),python3)

.PHONY: help env-init validate test lint devtools provision dist bundle install-units \
        install-config keys wg-render authz-verify anchor guard-status guard-contract \
        peer-manifest containment wiki-preflight wiki-status wiki-lint wiki-capture clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env-init: ## Write this role's .env + wiki dirs from .env.example (idempotent)
	@$(FABRIC_ROOT)/scripts/sparknet-env-init.sh "$(VAULT_ROOT)"

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
		echo "ruff not installed — skipping lint."; \
		echo "Run 'make devtools' to install it into .venv."; \
		echo "(ruff is optional; the suite runs without it.)"; \
	fi

devtools: ## Create .venv with cryptography + pytest + ruff (opt-in)
	@if [ -x "$(VENV_PY)" ]; then \
		echo "$(VENV_PY) already exists; refreshing deps"; \
	else \
		echo "Creating $(VAULT_ROOT)/.venv"; \
		python3 -m venv "$(VENV_PY)"; \
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

install-units: ## Generate shims + install systemd units (dry-run; pass APPLY=1 to write)
	@./scripts/sparknet-install-units.sh $(if $(APPLY),--apply,)

install-config: ## Show the /etc config diff. Never writes, by design.
	@./scripts/sparknet-install-config.sh

# --- Master-anchored node operations ----------------------------------------

KEY_OUT ?= $(VAULT_ROOT)/.state/keys

keys: ## Generate WireGuard + Ed25519 keypairs into $(KEY_OUT) (secrets, never committed)
	@./scripts/sparknet-keygen.sh --role node --out "$(KEY_OUT)"

wg-render: ## Render wg0.conf from the template. Dry-run; pass APPLY=1 to write at 0600
	@./scripts/sparknet-wireguard-render.sh $(if $(APPLY),--apply,)

authz-verify: ## Verify this node's identity against the signed registry
	@./scripts/sparknet-run python3 ./services/sparknet-authz.py verify \
		"$(if $(IDENTITY),$(IDENTITY),manifests/node-identity.json)" \
		--registry "$(if $(REGISTRY),$(REGISTRY),manifests/node-authorizations.json)" \
		--master-pubkey "$(if $(MASTER_PUBKEY),$(MASTER_PUBKEY),manifests/keys/master-ed25519.pub.b64)"

anchor: ## Bring up the WireGuard Master Anchor (root; render + wg-quick up + chattr +i)
	@./scripts/sparknet-wireguard-anchor.sh

containment: ## THIS NODE's root-gated teardown. Not the cluster KillSwitch (Master-side).
	@echo "This tears down only this node's anchor. The cluster KillSwitch is a Master"
	@echo "action and is specified in master-node/docs/master-killswitch.md — it does not"
	@echo "exist yet. Continuing with node-local containment in 3s (Ctrl-C to abort)."
	@sleep 3
	@./scripts/sparknet-node-containment.sh $(if $(DRY_RUN),--dry-run,)

guard-status: ## Report the guard's manifest state and authorization posture
	@./scripts/sparknet-run python3 ./services/sparknet-guard-d.py --check-manifest

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

clean: ## Remove caches and build output
	@find "$(VAULT_ROOT)" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf "$(VAULT_ROOT)/.pytest_cache" "$(VAULT_ROOT)/dist"
	@echo "Cleaned caches and dist/ (left .venv, .state and .cache alone)."