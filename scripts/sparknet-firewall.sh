#!/usr/bin/env bash
# SPARKNET-FW — the node's host firewall.
#
# This is the rule the operator decision attaches to publishing a surface on
# `0.0.0.0`. The node's own chat surface answers on the LAN and the overlay; a
# host firewall keeps those ports off the public internet, on both address
# families. Binding is not the confidentiality control any more — this is.
#
# What it protects (the "exposed" set in manifests/ai-stack.json):
#   8181  — Open WebUI   (host netns, so INPUT)
#   11434 — Ollama       (host netns, so INPUT)
#
# Qdrant is NOT here. It binds 127.0.0.1:6333/6334 and is never published on
# the LAN or overlay; the fabric-facing vector surface remains the Master's
# signed 4223 listener. A loopback-only bind needs no host firewall rule.
#
# Allowed sources (manifests/ai-stack.json network_boundary.allowed_sources):
#   loopback, the LAN (192.168.178.0/24), the wg1 overlay (10.100.0.0/24),
#   the docker bridge subnets (172.16.0.0/12), and link-local.
#
# Mechanism: two custom chains, jumped to only for the protected ports, so the
# rest of the host is untouched (the node's default INPUT policy is ACCEPT and
# this script does not change it). Idempotent: safe to re-run; it flushes and
# rebuilds only its own chains, and re-checks its jumps with `-C`.
#
# Usage:
#   sparknet-firewall.sh install   # build + apply the rules (root)
#   sparknet-firewall.sh status    # show the chains (root)
#   sparknet-firewall.sh flush     # remove the chains and jumps (root)
set -euo pipefail

CHAIN_IN="SPARKNET-FW-IN"
CHAIN_FWD="SPARKNET-FW-FWD"

# The protected TCP ports. Kept in sync with manifests/ai-stack.json by
# tests/test_firewall.py — a port published without a rule is the failure mode
# this whole file exists to prevent.
PROTECTED_TCP=(8181 11434)

ALLOWED_V4=(
  "127.0.0.0/8"      # loopback
  "192.168.178.0/24" # LAN
  "10.100.0.0/24"    # wg1 overlay
  "172.16.0.0/12"    # docker bridges
  "169.254.0.0/16"   # link-local
)

# No docker v6 chains exist, so v6 is INPUT-only. Allow loopback and
# link-local; the LAN and overlay are reached over IPv4, and a LAN client that
# wants the surface over v6 can use its v4 address.
ALLOWED_V6=(
  "::1/128"     # loopback
  "fe80::/10"   # link-local
)

need_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "sparknet-firewall: must run as root" >&2
    exit 1
  fi
}

ensure_chain() {
  local family="$1" table="$2" chain="$3"
  "${family}" -t "${table}" -N "${chain}" 2>/dev/null || true
  # DOCKER-USER belongs to Docker and the operator; never flush it.
  if [[ "${chain}" != "DOCKER-USER" ]]; then
    "${family}" -t "${table}" -F "${chain}"
  fi
}

rebuild_chain() {
  # rebuild_chain <family> <chain> <allowed...>
  local family="$1" chain="$2"; shift 2
  ensure_chain "${family}" filter "${chain}"
  "${family}" -A "${chain}" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  local src
  for src in "$@"; do
    "${family}" -A "${chain}" -s "${src}" -j RETURN
  done
  "${family}" -A "${chain}" -j DROP
}

jump_for_ports() {
  # jump_for_ports <family> <parent_chain> <target_chain> <port...>
  local family="$1" parent="$2" target="$3"; shift 3
  local port
  for port in "$@"; do
    if ! "${family}" -C "${parent}" -p tcp --dport "${port}" -j "${target}" 2>/dev/null; then
      "${family}" -I "${parent}" 1 -p tcp --dport "${port}" -j "${target}"
    fi
  done
}

remove_jumps() {
  local family="$1" parent="$2" target="$3"; shift 3
  local port
  for port in "$@"; do
    while "${family}" -C "${parent}" -p tcp --dport "${port}" -j "${target}" 2>/dev/null; do
      "${family}" -D "${parent}" -p tcp --dport "${port}" -j "${target}"
    done
  done
}

install_rules() {
  need_root
  # INPUT: the host-netns surfaces (8181 webui, 11434 ollama).
  rebuild_chain iptables "${CHAIN_IN}" "${ALLOWED_V4[@]}"
  jump_for_ports iptables INPUT "${CHAIN_IN}" "${PROTECTED_TCP[@]}"

  # FORWARD/DOCKER-USER is kept for the host's docker-published surfaces. The
  # chain has to exist even when the current protected set is host-netns-only,
  # because a later published port is a one-line change and the jump must be
  # re-applied after docker restarts (the unit is ordered After=docker.service).
  ensure_chain iptables filter DOCKER-USER
  # A default RETURN so anything we did not match keeps traversing FORWARD.
  if ! iptables -C DOCKER-USER -j RETURN 2>/dev/null; then
    iptables -A DOCKER-USER -j RETURN
  fi
  rebuild_chain iptables "${CHAIN_FWD}" "${ALLOWED_V4[@]}"
  jump_for_ports iptables DOCKER-USER "${CHAIN_FWD}" "${PROTECTED_TCP[@]}"

  # IPv6: INPUT only (no docker v6 chains).
  rebuild_chain ip6tables "${CHAIN_IN}" "${ALLOWED_V6[@]}"
  jump_for_ports ip6tables INPUT "${CHAIN_IN}" "${PROTECTED_TCP[@]}"

  echo "sparknet-firewall: installed ${CHAIN_IN} (v4+v6) and ${CHAIN_FWD} (v4)"
  # Prove it took. `install` reporting success for a half-built state is the
  # failure this whole file exists to prevent, so the build verifies itself.
  verify_rules
}

flush_rules() {
  need_root
  remove_jumps iptables INPUT "${CHAIN_IN}" "${PROTECTED_TCP[@]}"
  remove_jumps ip6tables INPUT "${CHAIN_IN}" "${PROTECTED_TCP[@]}"
  remove_jumps iptables DOCKER-USER "${CHAIN_FWD}" "${PROTECTED_TCP[@]}"
  iptables -F "${CHAIN_IN}" 2>/dev/null || true
  iptables -X "${CHAIN_IN}" 2>/dev/null || true
  iptables -F "${CHAIN_FWD}" 2>/dev/null || true
  iptables -X "${CHAIN_FWD}" 2>/dev/null || true
  ip6tables -F "${CHAIN_IN}" 2>/dev/null || true
  ip6tables -X "${CHAIN_IN}" 2>/dev/null || true
  echo "sparknet-firewall: flushed"
}

show_status() {
  need_root
  echo "== iptables INPUT (${CHAIN_IN} jumps) =="
  iptables -S INPUT | grep -- "${CHAIN_IN}" || echo "  (none)"
  echo "== iptables ${CHAIN_IN} =="
  iptables -S "${CHAIN_IN}" 2>/dev/null || echo "  (absent)"
  echo "== iptables DOCKER-USER (${CHAIN_FWD} jumps) =="
  iptables -S DOCKER-USER 2>/dev/null | grep -- "${CHAIN_FWD}" || echo "  (none)"
  echo "== iptables ${CHAIN_FWD} =="
  iptables -S "${CHAIN_FWD}" 2>/dev/null || echo "  (absent)"
  echo "== ip6tables INPUT (${CHAIN_IN} jumps) =="
  ip6tables -S INPUT | grep -- "${CHAIN_IN}" || echo "  (none)"
  echo "== ip6tables ${CHAIN_IN} =="
  ip6tables -S "${CHAIN_IN}" 2>/dev/null || echo "  (absent)"
}

verify_rules() {
  # Prove the enforcement is actually present, on both families and both
  # parents. `install` builds the rules; on a host missing this service (or if
  # a docker restart dropped the FORWARD jump), nothing re-checks. A boot that
  # runs `install` without this would report success for a half-built state.
  need_root
  local failures=()
  local port

  for port in "${PROTECTED_TCP[@]}"; do
    iptables -C INPUT -p tcp --dport "$port" -j "${CHAIN_IN}" 2>/dev/null \
      || failures+=("ipv4 INPUT --dport ${port} has no ${CHAIN_IN} jump")
    ip6tables -C INPUT -p tcp --dport "$port" -j "${CHAIN_IN}" 2>/dev/null \
      || failures+=("ipv6 INPUT --dport ${port} has no ${CHAIN_IN} jump")
    iptables -C DOCKER-USER -p tcp --dport "$port" -j "${CHAIN_FWD}" 2>/dev/null \
      || failures+=("ipv4 DOCKER-USER --dport ${port} has no ${CHAIN_FWD} jump")
  done

  # Each chain must end in a DROP, or traffic reaching it is not actually denied.
  iptables -S "${CHAIN_IN}" 2>/dev/null | grep -q -- "-j DROP" \
    || failures+=("ipv4 ${CHAIN_IN} has no terminal DROP")
  ip6tables -S "${CHAIN_IN}" 2>/dev/null | grep -q -- "-j DROP" \
    || failures+=("ipv6 ${CHAIN_IN} has no terminal DROP")
  iptables -S "${CHAIN_FWD}" 2>/dev/null | grep -q -- "-j DROP" \
    || failures+=("ipv4 ${CHAIN_FWD} has no terminal DROP")

  if (( ${#failures[@]} > 0 )); then
    echo "sparknet-firewall: VERIFY FAILED — the host firewall is not in force:" >&2
    printf '  - %s\n' "${failures[@]}" >&2
    return 1
  fi
  echo "sparknet-firewall: verified — ${#PROTECTED_TCP[@]} protected TCP ports jump to ${CHAIN_IN}/${CHAIN_FWD} on v4+v6, chains end in DROP"
}

case "${1:-}" in
  install) install_rules ;;
  flush)   flush_rules ;;
  status)  show_status ;;
  verify)  verify_rules ;;
  --help|-h)
    echo "Usage: $0 {install|status|verify|flush}"
    echo
    echo "SPARKNET-FW — the node's host firewall. Keeps the published AI-stack"
    echo "ports (8181, 11434) off the public internet while leaving the"
    echo "rest of the host's traffic alone."
    echo "  install   build + apply the SPARKNET-FW-IN and SPARKNET-FW-FWD chains (root)"
    echo "  status    show the chains and where they are jumped from (root)"
    echo "  verify    exit non-zero unless every protected port is enforced (root)"
    echo "  flush     remove the chains and their jumps (root)"
    echo "Allowed sources: loopback, LAN 192.168.178.0/24, overlay 10.100.0.0/24,"
    echo "docker bridges 172.16.0.0/12, link-local. IPv6: loopback + link-local only."
    echo "Idempotent: safe to re-run. install verifies itself."
    exit 0
    ;;
  *)
    echo "Usage: $0 {install|status|verify|flush}" >&2
    exit 2
    ;;
esac
