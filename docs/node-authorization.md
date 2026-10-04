# Node authorization

WireGuard proves a peer holds the right key. It does **not** say *which node* that
key belongs to or *what it is allowed to do*. That is this layer.

> **Correction worth keeping:** WireGuard has no SHA-256 in its protocol. SHA-256
> here is the authorization fingerprint; Ed25519 is what signs it. Anyone
> describing this as "SHA-256 WireGuard authentication" is wrong.

## Identity and fingerprint

`manifests/node-identity.json` is the node's identity. Its canonical fields are
hashed with SHA-256 to form the fingerprint:

```
node_id, role, wireguard_public_key_b64, overlay_ipv4, requested_capabilities
```

Canonical JSON, sorted keys, no whitespace — so the same identity always yields
the same fingerprint on any machine.

```bash
python3 services/sparknet-authz.py fingerprint manifests/node-identity.json
```

## The registry

`manifests/node-authorizations.json` holds Master-signed entries. Each entry is
signed over the fingerprint, identity, capabilities, overlay address, issue and
expiry, and a revoked flag.

```bash
# Master side. The Master private key never leaves the Master.
python3 services/sparknet-authz.py sign manifests/node-identity.json \
  --master-key /path/to/master-ed25519.key \
  --capabilities telemetry,p2p-sync,guard-apply \
  --out entry.json

# Node side.
make authz-verify
```

## Capabilities

| Capability     | Grants                                                        |
|----------------|---------------------------------------------------------------|
| `telemetry`    | push metrics to the Master                                     |
| `p2p-sync`     | accept and serve sync from other nodes                          |
| `push-receive` | accept pushed artifacts                                         |
| `guard-apply`  | **the sensitive one** — apply Master-approved changes to protected files |

`guard-apply` is the bridge to [the guard](wireguard.md): without it, the guard
verifies and audits but refuses to apply, regardless of how valid a signature is.

## Revocation

The Master signs `revoked: true` and redistributes the registry. Verification
checks revocation **before** signature validity, so a tampered registry fails
closed: removing the `revoked` flag breaks the signature, so a hand-edited
registry cannot quietly un-revoke a node.

## Bootstrap caveat

The tracked `manifests/keys/master-ed25519.pub.b64` was generated for structure
only and its private key was discarded. It cannot sign anything. Before real
provisioning you must either:

- generate the Master's keypair and **replace** the tracked public key, or
- keep the tracked key and regenerate a matching private key, which is not
   possible here.

Until then the registry ships empty on purpose: no node gets privileges by
default. See `manifests/security-policies.json` for the machine-readable policy.

## What is not proven here

The authorization *logic* is fully covered (26 tests in `tests/test_authz.py`):
fingerprint stability, signature verification, tampering on identity /
capabilities / overlay / expiry, revocation, unknown fingerprints, and
master-key-only signing. What is **not** covered is the live provisioning run
against a real Master keypair and a real registry distribution channel.