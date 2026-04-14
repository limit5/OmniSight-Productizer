#!/usr/bin/env bash
# Phase 64-B — iptables hardening for the Tier-2 networked sandbox.
#
# Tier 2's policy is the inverse of Tier 1: ACCEPT outbound to the
# public internet, DROP outbound to private / link-local / unique-local
# addresses. This stops a prompt-injected agent from pivoting onto the
# corporate LAN even though it has internet access.
#
# Run ONCE on each host that runs Tier-2 sandboxes. Idempotent
# (flushes its own chain before re-installing).
#
# Requires: root (CAP_NET_ADMIN), iptables, ip6tables, docker.

set -euo pipefail

NETWORK_NAME="omnisight-egress-t2"
CHAIN4="OMNISIGHT-T2-EGRESS"
CHAIN6="OMNISIGHT-T2-EGRESS6"

if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    echo "Docker network '$NETWORK_NAME' not found." >&2
    echo "Either start a T2 container first, or run:" >&2
    echo "  docker network create --driver bridge $NETWORK_NAME" >&2
    exit 3
fi

BRIDGE_IF=$(docker network inspect "$NETWORK_NAME" \
    --format '{{(index .Options "com.docker.network.bridge.name")}}')
[[ -z "$BRIDGE_IF" ]] && BRIDGE_IF="br-$(docker network inspect $NETWORK_NAME --format '{{.Id}}' | cut -c1-12)"

echo "Installing iptables rules for $NETWORK_NAME (bridge: $BRIDGE_IF)"

# ── IPv4: DROP RFC1918 + link-local + loopback + multicast ──
iptables -N "$CHAIN4" 2>/dev/null || iptables -F "$CHAIN4"

# Return traffic for outbound connections we initiated.
iptables -A "$CHAIN4" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Block loopback rebinding.
iptables -A "$CHAIN4" -d 127.0.0.0/8 -j DROP
# RFC1918 private ranges.
iptables -A "$CHAIN4" -d 10.0.0.0/8 -j DROP
iptables -A "$CHAIN4" -d 172.16.0.0/12 -j DROP
iptables -A "$CHAIN4" -d 192.168.0.0/16 -j DROP
# Carrier-grade NAT (RFC6598) — internal infra often hides here.
iptables -A "$CHAIN4" -d 100.64.0.0/10 -j DROP
# Link-local + multicast + reserved.
iptables -A "$CHAIN4" -d 169.254.0.0/16 -j DROP
iptables -A "$CHAIN4" -d 224.0.0.0/4 -j DROP
iptables -A "$CHAIN4" -d 240.0.0.0/4 -j DROP
# Default ACCEPT (public internet).
iptables -A "$CHAIN4" -j ACCEPT

iptables -D FORWARD -i "$BRIDGE_IF" -j "$CHAIN4" 2>/dev/null || true
iptables -I FORWARD -i "$BRIDGE_IF" -j "$CHAIN4"

# ── IPv6: DROP ULA + link-local + loopback + multicast ──
ip6tables -N "$CHAIN6" 2>/dev/null || ip6tables -F "$CHAIN6"

ip6tables -A "$CHAIN6" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A "$CHAIN6" -d ::1/128 -j DROP
ip6tables -A "$CHAIN6" -d fc00::/7 -j DROP        # ULA
ip6tables -A "$CHAIN6" -d fe80::/10 -j DROP       # link-local
ip6tables -A "$CHAIN6" -d ff00::/8 -j DROP        # multicast
ip6tables -A "$CHAIN6" -j ACCEPT

ip6tables -D FORWARD -i "$BRIDGE_IF" -j "$CHAIN6" 2>/dev/null || true
ip6tables -I FORWARD -i "$BRIDGE_IF" -j "$CHAIN6"

echo "Done. Tier-2 egress: public ACCEPT, RFC1918/ULA/link-local DROP."
