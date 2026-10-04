#!/usr/bin/env bash
# Vault/path/venv-agnostic environment setup — peer role.
#
# SPARKNET_VAULT_ROOT resolves to *this subvault*, not the parent. That is what
# makes the peer independently runnable: nothing here reaches up into the parent
# to find a file it depends on.
#
# The parent vault holds `contracts/` (values both roles agree on) and
# `shared/` (code both roles execute). Those are reached by an explicit,
# overridable path, never by an assumption about where the parent lives:
#
#   SPARKNET_FABRIC_ROOT   parent vault root (contracts + shared)
#
# If the peer is deployed on its own without the parent, point these two at
# wherever the contracts and shared code were installed. See
# docs/portability.md.
set -euo pipefail

SPARKNET_ENV_SELF="${BASH_SOURCE[0]}"
SPARKNET_SCRIPTS_DIR="$(cd "$(dirname "$SPARKNET_ENV_SELF")" && pwd)"

# The peer subvault root: the parent of scripts/.
SPARKNET_VAULT_ROOT="${SPARKNET_VAULT_ROOT:-$(dirname "$SPARKNET_SCRIPTS_DIR")}"
SPARKNET_ROLE="peer"

# The parent, holding the shared contract and shared code. Defaults to the
# sibling relationship (parent/peer-node -> parent) but is fully overridable so
# a standalone deployment can point elsewhere.
SPARKNET_FABRIC_ROOT="${SPARKNET_FABRIC_ROOT:-$(dirname "$SPARKNET_VAULT_ROOT")}"
SPARKNET_CONTRACTS_DIR="${SPARKNET_CONTRACTS_DIR:-$SPARKNET_FABRIC_ROOT/contracts}"
SPARKNET_SHARED_DIR="${SPARKNET_SHARED_DIR:-$SPARKNET_FABRIC_ROOT/shared}"

SPARKNET_KNOWLEDGE_ROOT="${SPARKNET_KNOWLEDGE_ROOT:-$(dirname "$SPARKNET_VAULT_ROOT")}"
VAULT_PATH="${VAULT_PATH:-$SPARKNET_VAULT_ROOT}"

# Runtime state stays inside this subvault unless overridden.
SPARKNET_STATE_DIR="${SPARKNET_STATE_DIR:-$SPARKNET_VAULT_ROOT/.state}"
SPARKNET_CACHE_DIR="${SPARKNET_CACHE_DIR:-$SPARKNET_VAULT_ROOT/.cache}"

# Default XDG dirs inside the subvault so nothing leaks onto the host. A
# pre-existing XDG_* in the environment still wins — that is a deliberate
# host-level setting, not an oversight.
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$SPARKNET_STATE_DIR/config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$SPARKNET_STATE_DIR/share}"
XDG_STATE_HOME="${XDG_STATE_HOME:-$SPARKNET_STATE_DIR}"

export SPARKNET_VAULT_ROOT SPARKNET_KNOWLEDGE_ROOT SPARKNET_ROLE
export SPARKNET_FABRIC_ROOT SPARKNET_CONTRACTS_DIR SPARKNET_SHARED_DIR
export SPARKNET_STATE_DIR SPARKNET_CACHE_DIR
export VAULT_PATH
export XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME
