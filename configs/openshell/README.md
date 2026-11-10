# OpenShell gateway (peer)

The peer runs one OpenShell gateway on loopback. It owns the per-user
execution sandboxes and is the only process that talks to the Docker daemon.

## Install

1. Install the pinned OpenShell v0.1.7 package for this host. Confirm
   `command -v openshell-gateway` and `command -v openshell`. The package
   checksum and image digests are recorded in `manifests/openshell.json`.
2. Render the VM gateway config from the owner and the pin manifest:

   ```sh
   python3 scripts/sparknet-openshell-config.py --owner "$USER"
   ```

   This writes `~/.config/openshell/gateway.toml`, `owner.json` and
   `gateway.env` (0600) with the owner UID/GID and digest-pinned images. It is
   also the identity record the engine checks before creating a sandbox. Use
   `--out-dir` when rendering for another owner. For the Docker-driver config,
   copy `gateway.toml` manually; it has no owner-specific paths.
3. Enable linger so the gateway and its sandboxes survive logout:

   ```sh
   loginctl enable-linger "$USER"
   ```

4. The official installer already creates and enables the package user service
   `openshell-gateway.service`. Restart it after rendering the config:

   ```sh
   systemctl --user daemon-reload
   systemctl --user restart openshell-gateway
   ```

5. Register the package-managed local gateway once:

   ```sh
   openshell gateway add https://127.0.0.1:17670 --local --name openshell
   openshell status
   ```

   The package writes the mTLS client bundle under
   `~/.config/openshell/gateways/<name>/mtls/`. The engine reads that bundle;
   it is never copied into a sandbox.

## Known platform blocker (2026-11-10)

OpenShell v0.1.7 requires **Docker 28.0+** and **Landlock ABI 3 (Linux 6.2+)**
for the Docker driver, and its embedded **libkrun** requires **GLIBC 2.33** for
the VM driver. The current `agx32` and `agx64` hosts run Docker 26.1.3, kernel
5.10.216-tegra without Landlock, and glibc 2.31. The gateway installs, starts,
authenticates over mTLS and passes `config preflight`, but no sandbox can run.
Do not switch the engine to `--sandbox-backend openshell` on these hosts.

`gateway-vm.toml.in` is the validated microVM template for a supported host
(Docker 28+, kernel 6.2+ with Landlock, glibc 2.33+, `/dev/kvm`). It is rendered
by `scripts/sparknet-openshell-config.py`; do not copy the template directly.

## Engine credential note

When the engine runs as root, set `SPARKNET_OWNER_USER` (and `--openshell-user`
if needed) so the OpenShell CLI runs as the gateway owner. The mTLS bundle lives
under that user's `~/.config/openshell/gateways/<name>/mtls/`; a root process
without the owner's `HOME` cannot authenticate. The engine also reads
`~/.config/openshell/owner.json` and refuses a sandbox whose resolved
`WorkloadIdentity` does not match the owner.

## Why not root

The gateway only needs the Docker socket, not root. Running it as the owner
keeps the user-auth boundary real: an unauthenticated root gateway on loopback
would let any local user create a rootful-Docker sandbox. The Docker daemon
stays rootful, so container UID `N` still maps to host UID `N`.

## Check

```sh
OPENSHELL_GATEWAY_CONFIG=~/.config/openshell/gateway.toml openshell-gateway config preflight
systemctl --user status openshell-gateway.service
openshell status
```

The gateway must never bind a non-loopback address on the peer. The Master-facing
`/sandbox/*` route is reserved and not implemented (peer PLAN 20E); the gateway
is reached only by the local engine.
