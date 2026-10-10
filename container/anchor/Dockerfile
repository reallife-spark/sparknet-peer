# The sparknet anchor — the long-lived tunnel sidecar.
#
# The wireguard tunnel belongs to ONE long-lived container: the anchor. The
# node's opencode container joins the anchor's network namespace
# (`network_mode: service: anchor` in configs/container/docker-compose.yml), so
# recreating the app leaves the tunnel intact. Killing the anchor drops the
# overlay, which is exactly the property the node relies on — the tunnel must
# outlive the app, never the other way around.
#
# wireguard-go is built from source (no opaque prebuilt binary), cross-compiled
# by buildx for the target architecture. The build stage runs on
# $BUILDPLATFORM (native to the build machine, no qemu) and GOARCH=$TARGETARCH
# cross-compiles. Source is the official GitHub mirror pinned by tag — the
# vanity domain golang.zx2c4.com has no module metadata (404), so the canonical
# `go install golang.zx2c4.com/...` fetch cannot work; the mirror serves the
# same commit (21636207). The container commits to the userspace
# implementation on purpose: the deployed hosts have CONFIG_WIREGUARD=n, so a
# kernel-gated tunnel would not work there. `cap_add: NET_ADMIN,NET_RAW` plus
# /dev/net/tun are the anchor's full privilege surface; it has no other rights
# and no access to any key material — the wireguard config is mounted read-only
# and holds the host-rendered peer config, never a signing key.

FROM --platform=$BUILDPLATFORM golang:1.22-alpine AS build
ARG TARGETARCH
ENV CGO_ENABLED=0
# Pinned to the v0.0.20230223 line (tag 0.0.20230223, commit 21636207). The
# Alpine stage has no compile toolchain: the final image is a pure copy.
RUN apk add --no-cache git make \
 && git clone --depth 1 --branch 0.0.20230223 https://github.com/WireGuard/wireguard-go /src \
 && cd /src \
 && GOARCH="${TARGETARCH}" make

FROM alpine:3.20
RUN apk add --no-cache ca-certificates iproute2 tini wireguard-tools
COPY --from=build /src/wireguard-go /usr/local/bin/wireguard-go
COPY anchor/entrypoint.sh /usr/local/bin/sparknet-anchor
RUN chmod 755 /usr/local/bin/wireguard-go /usr/local/bin/sparknet-anchor \
 && wireguard-go --version

# `wgcore` is the anchor's interface name: distinct from the host's wg0/wg1 so
# a node inside the container can never collide with the host's own overlay
# interfaces when both exist.
# `/sbin/tini`: Alpine puts tini in /sbin, never /usr/bin (a rolling gap between
# the two init layouts has bitten this image before).
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/sparknet-anchor"]