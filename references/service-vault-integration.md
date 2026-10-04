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
