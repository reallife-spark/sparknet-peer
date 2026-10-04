# User Local System Identity & Privilege Scoping

## Purpose
Ensure agentic commands are accurate to the user's environment and strictly bound to user permissions. API calls originate from the user computer to the node (relay model).

## Identity Capture
Run `scripts/sparknet-identity-capture.sh` to generate `manifests/user-env-manifest.json` documenting:
- User identity (uid/euid/gid/egid, username, home, shell, groups)
- Environment (XDG dirs, PATH)
- System (hostname, OS, platform, release, machine)
- Privilege boundaries

## Enforcement Principles
1. Default context: operations execute under detected user context (UID/GID). No automatic privilege escalation.
2. Root-only actions explicitly gated: KillSwitch, chattr ±i, immutable guard require signed Master approvals and root execution path.
3. Credential safety: respect XDG/keyrings, never wholesale exfiltration, read-only by default, least privilege.
4. API relay model: calls originate from user machine → node (localhost/LAN/WG), preserving local context.
5. Vault-scoped isolation: explicit env, no global PATH pollution, absolute paths.
