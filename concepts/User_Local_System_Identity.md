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
