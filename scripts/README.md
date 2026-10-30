# scripts/

The peer's operational scripts: 23 programs, all `bash` with
`set -euo pipefail` (except the two `.py` helpers), all answering `--help`
**and changing nothing when they do** (`tests/test_shell.py`), and all
resolving their paths from their own location (`sparknet-env.sh`), so the role
is relocatable.

| Group | Scripts |
|---|---|
| Environment and identity | `sparknet-env.sh`, `sparknet-keygen.sh`, `sparknet-identity-capture.sh`, `sparknet-run` |
| Overlay | `sparknet-wireguard-anchor.sh`, `sparknet-wireguard-render.sh`, `sparknet-wg-render-peer.sh`, `sparknet-wg-bringup.sh` |
| Host hardening | `sparknet-firewall.sh`, `sparknet-lockdown.sh`, `sparknet-wlan-guard.sh`, `sparknet-chattr-preflight.sh`, `sparknet-node-containment.sh` |
| Registry and election | `sparknet-registry-fetch.sh`, `sparknet-registry-verify-peer.sh`, `sparknet-election-request.sh` |
| Lifecycle | `sparknet-provision.sh`, `sparknet-validate.sh`, `sparknet-install-config.sh`, `sparknet-install-units.sh`, `sparknet-sync.sh` |
| Knowledge and model | `sparknet-masterspike.py`, `sparknet-webui-workspace.py` |

Two rules are enforced rather than documented:

- **Mutating scripts fail closed and say what they refused.** The install and
  render scripts are dry-run by default; `APPLY=1` is the write.
- **`/etc` is derived from the vault, never the other way round.**
  `sparknet-install-config.sh` prints the diff and cannot write it.

`sparknet-node-containment.sh` tears down **this node only**. The cluster-wide
KillSwitch is Master-side; the peer has no copy and no target for it.
