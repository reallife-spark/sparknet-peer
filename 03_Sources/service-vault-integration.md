---
title: "Service/Vault Integration: Path/VEnv/Vault-Agnostic Design"
updated: 2026-10-06
tracking:
  schema: sparknet-tracking/1
  owner_id: U-27689
  owner_node: sparknet-node-0001
  owner_ref: 00_System/user.md
  validity:
    state: signed
  conversation: []
  shares: []
  agent: opencode
  model: big-pickle
  approvals: []
  signature:
    alg: ed25519
    note_version: sparknet-note/2
    sig: h7IVreBGbNFBuWefzy49AOTqO8eGNPKlGRNusQjt071/UNWEHJshv0dgaPSrT2OU8o4K0RLWn7CRGnUlqUUNBg==
    key_id: U-27689
    key_fingerprint: sha256:b4321f7ae5e41618306a4cc1e5f101771a8dc6032d6b7ac426702bb960af078e
    signed_body_sha256: ef2f1f358d5213ebe0fb036579a21f2e63b24b9205be21ef74da3124f91e26ec
    signed_meta_sha256: 6e2a556630aecad1d263a70e84b8eeaf2cd22731a3b303ce9f5f200c2097582a
    signed_tracking_sha256: baeb5e290d3300ec5c8c3d955eb7fbc4b6d0a1228b627f1e4e699502579ad0b5
    signed_conversation_root: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    signed_meta:
      title: "Service/Vault Integration: Path/VEnv/Vault-Agnostic Design"
      updated: 2026-10-06
    signed_at: 2026-10-13T02:14:36.412376Z
---
# Service/Vault Integration: Path/VEnv/Vault-Agnostic Design

## Problem statement recap
Services must work with vault switching without being directly in PATH or assuming fixed locations. Need to redesign so services stay working while not being directly in the path.

## Design principles
1. No PATH/CWD assumptions - use absolute paths everywhere
2. Explicit environment per context (vault-scoped)
3. Wrapper pattern: thin wrappers set up env and exec
4. Separation: root-only components (KillSwitch, immutable guard) vs user-space (sync/timers)
5. Vault-scoped isolation, no global pollution

## Environment variables
- SPARKNET_VAULT_ROOT: absolute path to sparknet root (e.g., /mnt/knowledge/sparknet)
- SPARKNET_KNOWLEDGE_ROOT: /mnt/knowledge
- VAULT_PATH: current vault path
- XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_STATE_HOME
- VIRTUAL_ENV, PYTHONPATH (explicit if using venv)
- SPARKNET_USER: detected user identity

## Wrapper pattern
sparknet-* wrapper scripts:
1. Source /etc/default/sparknet-env or vault-scoped env
2. Detect user context if needed
3. Set absolute paths
4. Exec target with clean, explicit env

## Systemd units
- Root units: WorkingDirectory set, EnvironmentFile/Environment explicit, no reliance on shell PATH
- User units (--user): run in user context, respect XDG, use absolute paths
