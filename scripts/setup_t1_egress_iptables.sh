#!/usr/bin/env bash
# Phase 64-A S2 — iptables hardening for the Tier-1 egress bridge.
#
# Run this ONCE on each host that runs Tier-1 sandboxes with egress
# enabled. It restricts outbound traffic from the omnisight-egress-t1
# docker bridge to the IPs that resolve from
# OMNISIGHT_T1_EGRESS_ALLOW_HOSTS, and DROPs everything else.
#
# Re-run after the allow-list changes — it is idempotent (flushes its
# own chain before re-installing).
#
# Requires: root (CAP_NET_ADMIN), iptables, getent.

set -euo pipefail

NETWORK_NAME="omnisight-egress-t1"
CHAIN="OMNISIGHT-T1-EGRESS"
HOSTS_RAW="${OMNISIGHT_T1_EGRESS_ALLOW_HOSTS:-}"

if [[ -z "$HOSTS_RAW" ]]; then
    echo "OMNISIGHT_T1_EGRESS_ALLOW_HOSTS is empty — refusing to install"
    echo "an empty allow-list (would silently DROP all egress)." >&2
    exit 2
fi

if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    echo "Docker network '$NETWORK_NAME' not found." >&2
    echo "Start a sandbox with OMNISIGHT_T1_ALLOW_EGRESS=true first," >&2
    echo "or run: docker network create --driver bridge $NETWORK_NAME" >&2
    exit 3
fi

# Resolve hosts (strip optional :port suffix).
declare -a IPS=()
IFS=',' read -ra ENTRIES <<< "$HOSTS_RAW"
for entry in "${ENTRIES[@]}"; do
    host="${entry%%:*}"
    host="${host// /}"
    [[ -z "$host" ]] && continue
    while read -r ip; do
        [[ -n "$ip" ]] && IPS+=("$ip")
    done < <(getent ahosts "$host" | awk '{print $1}' | sort -u)
done

if [[ ${#IPS[@]} -eq 0 ]]; then
    echo "No IPs resolved from allow-list — refusing to install (would DROP all)." >&2
    exit 4
fi

echo "Installing iptables rules for $NETWORK_NAME → ${#IPS[@]} unique IP(s)"

# Reset our private chain.
iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"

# Allow established/related (return traffic).
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Allow our resolved IPs.
for ip in "${IPS[@]}"; do
    iptables -A "$CHAIN" -d "$ip" -j ACCEPT
done
# DENY everything else.
iptables -A "$CHAIN" -j DROP

# Hook OUR chain into FORWARD for the bridge interface.
BRIDGE_IF=$(docker network inspect "$NETWORK_NAME" \
    --format '{{(index .Options "com.docker.network.bridge.name")}}')
[[ -z "$BRIDGE_IF" ]] && BRIDGE_IF="br-$(docker network inspect $NETWORK_NAME --format '{{.Id}}' | cut -c1-12)"

# Replace any prior FORWARD jump.
iptables -D FORWARD -i "$BRIDGE_IF" -j "$CHAIN" 2>/dev/null || true
iptables -I FORWARD -i "$BRIDGE_IF" -j "$CHAIN"

echo "Done. Bridge: $BRIDGE_IF, allow-list: ${IPS[*]}"
