# The sparknet peer-node container — the base node image.
#
# Built by `make container-build` from the repo's OWN bundle (`make dist`), so
# what runs inside the container is exactly what the bundle ships — one code
# path between the repo, the archive, and the image. This image is the opencode
# node itself. The AI stack (ollama + open-webui) is deliberately a SEPARATE
# services compose (`../configs/ai-stack`); this image is neither an AI service
# nor a GPU image.
#
# Layout contract (peer-node/scripts/sparknet-env.sh):
#   /srv/sparknet/            fabric root  -> SPARKNET_FABRIC_ROOT (path default)
#   /srv/sparknet/peer-node   this node    -> SPARKNET_VAULT_ROOT
#   /srv/sparknet/contracts   the contract -> SPARKNET_CONTRACTS_DIR
#   /srv/sparknet/shared      canonical    -> SPARKNET_SHARED_DIR
#
# The bundle reproduces that layout (`sparknet-peer/` extracted and renamed to
# `sparknet/`). No vault path is baked: `.env` is regenerated at boot by the
# entrypoint (idempotent `sparknet-env-init.sh`), so a container rebuilt from
# the same layers points at its own /srv/sparknet/peer-node.
#
# GPU is never baked. Docker cannot partition VRAM; model-server GPU sharing is
# configured inside the inference service on a node that has one
# (../configs/ai-stack README). A GPU is never required to *run* this image, and
# nothing in it references nvidia, cuda or cuDNN.

FROM python:3.12-slim-bookworm

# Pinned, never 'latest': a mutating tag is exactly the reproducibility
# guarantee a node image exists for.
ARG OPENCODE_VERSION=1.18.35
# Set by buildkit per --platform; maps to the release asset suffix.
ARG TARGETARCH

ENV OPENCODE_VERSION=${OPENCODE_VERSION} \
    SPARKNET_NODE_DIR=/srv/sparknet/peer-node \
    PATH="/srv/sparknet/.venv/bin:/usr/local/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

# The opencode binary, per architecture, from the pinned release. The archive
# carries a single top-level `opencode` binary — the same layout the official
# installer relies on. Never 'latest': version is a build argument with a
# pinned default, so a rebuild is byte-identical unless someone changes it.
RUN case "$TARGETARCH" in \
      amd64) asset=x64 ;; \
      arm64) asset=arm64 ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${asset}.tar.gz" -o /tmp/opencode.tar.gz \
 && tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode \
 && chmod 755 /usr/local/bin/opencode \
 && rm /tmp/opencode.tar.gz \
 && opencode --version

WORKDIR /srv/sparknet

# The runtime dependency: cryptography (manylinux wheels exist for both
# architectures; no build toolchain enters the image).
COPY requirements.txt /srv/sparknet/requirements.txt
RUN python3 -m venv /srv/sparknet/.venv \
 && /srv/sparknet/.venv/bin/pip install --no-cache-dir -r /srv/sparknet/requirements.txt

# The assembled node bundle, staged by `make container-build` at
# peer-node/container/context/sparknet-peer.tar.gz. Extracted once at image
# build; the container never mutates it. The node's writable state lives under
# /srv/sparknet/peer-node/.state, which the compose file installs as a HOST bind
# mount (never a named volume) — see the preflight and configs/container.
ARG BUNDLE_TARBALL=peer-node/container/context/sparknet-peer.tar.gz
COPY ${BUNDLE_TARBALL} /tmp/sparknet-peer.tar.gz
RUN mkdir -p /tmp/bundle /srv/sparknet \
 && tar -xzf /tmp/sparknet-peer.tar.gz -C /tmp/bundle \
 && cp -a /tmp/bundle/sparknet-peer/. /srv/sparknet/ \
 && rm -rf /tmp/bundle /tmp/sparknet-peer.tar.gz \
 && test -f /srv/sparknet/peer-node/scripts/sparknet-env.sh \
 && test -d /srv/sparknet/contracts \
 && test -d /srv/sparknet/shared

# The node's entrypoint and healthcheck ride inside the bundle (peer-node/
# container/), but they run from a stable path, copied out at build time so a
# regenerating container never depends on the writable layer being intact.
COPY peer-node/container/entrypoint.sh  /usr/local/bin/sparknet-entrypoint
COPY peer-node/container/healthcheck.sh /usr/local/bin/sparknet-healthcheck
RUN chmod 755 /usr/local/bin/sparknet-entrypoint /usr/local/bin/sparknet-healthcheck

WORKDIR /srv/sparknet/peer-node

# In headless use this node is `opencode serve`; the TUI runs at a terminal the
# operator owns. The entrypoint regenerates .env first (nothing baked), then
# execs the command. tini reaps the opencode server so it cannot orphan children.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sparknet-entrypoint"]
CMD ["opencode", "serve", "--hostname", "127.0.0.1"]