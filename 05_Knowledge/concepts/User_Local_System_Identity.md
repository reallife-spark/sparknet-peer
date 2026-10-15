---
title: User Local System Identity & Privilege Scoping
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
    sig: k/bmJkZxlCQZGFeffI9GDw+606jby27X+0HUUf9vb5cmKJuwDJ7JWs3eQdGeCxBnNt86JWgmNcL460C48QsxCA==
    key_id: U-27689
    key_fingerprint: sha256:b4321f7ae5e41618306a4cc1e5f101771a8dc6032d6b7ac426702bb960af078e
    signed_body_sha256: 58309c7b1ce0643d463167147d20c6911857c0e5b938404f2ebc15a189cd19a1
    signed_meta_sha256: 01c6ef0e5c525be2c37bc14bf0a5a643c7b5f7437cf0c29d55af761e2795dd14
    signed_tracking_sha256: baeb5e290d3300ec5c8c3d955eb7fbc4b6d0a1228b627f1e4e699502579ad0b5
    signed_conversation_root: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    signed_meta:
      title: User Local System Identity & Privilege Scoping
      updated: 2026-10-06
    signed_at: 2026-10-13T02:14:35.609913Z
---
# User Local System Identity & Privilege Scoping

Critical requirement from Google Doc: agents must identify/document the local user system, operate strictly within user privileges, safely access user credentials, and have API calls originate from user computer → node (relay model).

## Core Principles
1. System introspection - detect real user (UID/EUID, groups, HOME, shell, XDG dirs), hostname, OS, kernel, capabilities
2. Privilege boundaries - default to invoking under detected user context; root-only actions explicitly gated with signed approvals
3. Credential handling - respect XDG/keyrings, never exfiltrate wholesale, read-only by default
4. API relay model - calls originate from user machine to node (preserves local context)
5. Vault-scoped env - isolated, no global PATH pollution

## Implementation
See scripts/sparknet-identity-capture.sh and docs/user-identity.md
