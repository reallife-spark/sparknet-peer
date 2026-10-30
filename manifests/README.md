# manifests/

What the peer **declares** — templates and role manifests, not files installed
to `/etc` (the `/etc` configuration is derived, and `../configs/` holds its
templates). Nothing here is live key material.

| File | Declares |
|---|---|
| `node-identity.json` | This node's identity and fingerprint (template). |
| `node-authorizations.json` | The Master-signed registry. The shipped file is **empty on purpose**: a fresh deployment grants nothing, and the guard refuses to bind without authority. |
| `node-template-manifest.json` | The templates the node ships. |
| `critical-manifest.json` | The protected paths and their provenance. The guard decides what is protected from this file — the Master cannot widen it. |
| `security-policies.json` | The peer's security policies (including the approval validity window). |
| `user-env-manifest.json` | The environment contract: the key set every `.env` must expose. |
| `share-regime.json` | The sharing regime's configuration. |
| `ai-stack.json` | The node's declared AI stack. |
| `agent-models.json` | The declared models. |
| `masterspike.json`, `masterspike/` | The MASTERSPIKE artefacts and their manifest. |
| `vault-openapi.json` | The vault's API surface. |
| `keys/` | Key **templates** only. Generated keys live in `.state/keys` at `0600`. |

`make keys-check` fails the build if real key material appears in this
directory. `tests/test_manifests.py` checks the critical-manifest provenance
map, and the registry is verified against the pinned Master key by
`sparknet-registry-verify-peer.sh`.
