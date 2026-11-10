# Per-user OpenCode sandbox runtime.
#
# The base image is the role's own node image (or the AGX twin), so the sandbox
# has the same shell/Python/git toolchain as the node without pulling a second
# distribution. The engine starts one container from this image per user, with
# exactly one host mount (`<vault>:/work`) and one isolated internal network.
#
# Build (the tarball is downloaded by `make -C peer-node opencode-runtime`):
#   docker build \
#     --build-arg BASE_IMAGE=sparknet-spark-node:dev \
#     --build-arg OPENCODE_VERSION=2.2.5 \
#     -t sparknet-opencode-runtime:2.2.5 \
#     peer-node/configs/opencode-runtime
ARG BASE_IMAGE=sparknet-spark-node:dev
FROM ${BASE_IMAGE}

ARG OPENCODE_VERSION=2.2.5
ARG TARGETARCH

LABEL org.opencontainers.image.title="sparknet-opencode-runtime"
LABEL org.opencontainers.image.description="Per-user OpenCode sandbox runtime for Sparknet"
LABEL org.opencontainers.image.version="${OPENCODE_VERSION}"

# The tarball is supplied in the build context by the Make target. Building
# from it (rather than a network fetch inside RUN) keeps the build reproducible
# and works on hosts whose Docker builder has no DNS.
COPY opencode-linux-${TARGETARCH}.tar.gz /tmp/opencode.tar.gz
RUN tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode \
 && chmod 0755 /usr/local/bin/opencode \
 && rm /tmp/opencode.tar.gz \
 && opencode --version

COPY unix-proxy.py /opt/sparknet/unix-proxy.py
RUN chmod 0755 /opt/sparknet/unix-proxy.py

ENV HOME=/home/opencode \
    XDG_CONFIG_HOME=/home/opencode/.config \
    XDG_DATA_HOME=/home/opencode/.local/share \
    XDG_CACHE_HOME=/home/opencode/.cache

WORKDIR /sandbox

# The engine overrides the entrypoint to start the Unix-socket bridge and
# OpenCode; the image default is still useful for a manual smoke test.
ENTRYPOINT ["/usr/local/bin/opencode"]
