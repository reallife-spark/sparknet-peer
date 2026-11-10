#!/usr/bin/env bash
# Build the pinned OpenCode runtime image for either the full node base or the
# slim microVM base. The tarball is a build input, not a tracked artifact; the
# script removes it after the build unless --keep-tarball is given.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/configs/opencode-runtime"
VERSION="${OPENCODE_RUNTIME_VERSION:-2.2.5}"
BASE=""
TAG=""
KEEP=0

usage() {
  cat <<'EOF'
Usage: sparknet-opencode-runtime.sh --base IMAGE --tag TAG [OPTIONS]

Build the OpenCode runtime image from configs/opencode-runtime.

Options:
  --base IMAGE       Base image (required)
  --tag TAG          Image tag (required)
  --version VERSION  OpenCode version (default 2.2.5)
  --dir PATH         Build context (default configs/opencode-runtime)
  --keep-tarball     Keep the downloaded OpenCode tarball in the build context
  --help             Print this help; writes nothing
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="${2:?--base needs a value}"; shift 2 ;;
    --tag) TAG="${2:?--tag needs a value}"; shift 2 ;;
    --version) VERSION="${2:?--version needs a value}"; shift 2 ;;
    --dir) DIR="${2:?--dir needs a value}"; shift 2 ;;
    --keep-tarball) KEEP=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$BASE" ]] || { echo "--base is required" >&2; exit 2; }
[[ -n "$TAG" ]] || { echo "--tag is required" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 2; }
[[ -d "$DIR" ]] || { echo "build context not found: $DIR" >&2; exit 2; }

case "$(uname -m)" in
  aarch64|arm64) asset=arm64; targetarch=arm64 ;;
  x86_64|amd64) asset=x64; targetarch=amd64 ;;
  *) echo "unsupported host arch: $(uname -m)" >&2; exit 2 ;;
esac

tarball="$DIR/opencode-linux-${targetarch}.tar.gz"
if [[ ! -s "$tarball" ]]; then
  echo "downloading opencode ${VERSION} (${asset})"
  curl -fsSL \
    "https://github.com/anomalyco/opencode/releases/download/v${VERSION}/opencode-linux-${asset}.tar.gz" \
    -o "$tarball"
fi
cleanup() {
  if [[ "$KEEP" -eq 0 ]]; then
    rm -f "$tarball"
  fi
}
trap cleanup EXIT

docker build \
  --build-arg BASE_IMAGE="$BASE" \
  --build-arg OPENCODE_VERSION="$VERSION" \
  -t "$TAG" \
  "$DIR"
docker run --rm --entrypoint opencode "$TAG" --version
