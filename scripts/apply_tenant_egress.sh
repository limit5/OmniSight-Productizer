#!/usr/bin/env bash
# M6 — apply per-tenant egress allow-list as iptables rules.
#
# Reads the per-tenant policy from the OmniSight DB (via the Python
# helper `python -m backend.tenant_egress emit-rules`) and installs
# iptables OUTPUT rules that ACCEPT only the resolved IPs for the given
# `--uid-owner <sandbox_uid>` and DROP everything else.
#
# Usage:
#   sudo ./scripts/apply_tenant_egress.sh --tenant t-foo --uid 12345
#   sudo ./scripts/apply_tenant_egress.sh --all
#
# Re-run after a policy change — the chain is idempotent (flush +
# re-install). Requires CAP_NET_ADMIN (root).

set -euo pipefail

CHAIN_PREFIX="OMNISIGHT-EGRESS"
ALL=0
TENANT=""
UID_OWNER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tenant) TENANT="$2"; shift 2 ;;
        --uid)    UID_OWNER="$2"; shift 2 ;;
        --all)    ALL=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2 ;;
    esac
done

PYTHON_BIN="${OMNISIGHT_PYTHON:-python3}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

apply_one() {
    local tid="$1"
    local uid="$2"
    if [[ -z "$tid" || -z "$uid" ]]; then
        echo "apply_one: tenant and uid required" >&2
        return 2
    fi

    local chain="${CHAIN_PREFIX}-$(echo "$tid" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | cut -c1-22)"
    local plan_json
    plan_json=$(cd "$REPO_ROOT" && "$PYTHON_BIN" -m backend.tenant_egress emit-rules \
        --tenant-id "$tid" --sandbox-uid "$uid")

    local default_action
    default_action=$(echo "$plan_json" | "$PYTHON_BIN" -c \
        'import json,sys; print(json.load(sys.stdin)["default_action"])')

    # Reset our private chain.
    iptables -N "$chain" 2>/dev/null || iptables -F "$chain"

    # Always allow established/related (return traffic + DNS responses).
    iptables -A "$chain" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # Append per-IP ACCEPT rules.
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local action dest label
        action=$(echo "$line"  | "$PYTHON_BIN" -c 'import json,sys; r=json.loads(sys.stdin.read()); print(r["action"])')
        dest=$(echo "$line"    | "$PYTHON_BIN" -c 'import json,sys; r=json.loads(sys.stdin.read()); print(r["destination"])')
        label=$(echo "$line"   | "$PYTHON_BIN" -c 'import json,sys; r=json.loads(sys.stdin.read()); print(r.get("label",""))')
        iptables -A "$chain" -d "$dest" -m comment \
            --comment "omnisight ${tid} ${label}" -j "$action"
    done < <(echo "$plan_json" | "$PYTHON_BIN" -c \
        'import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin)["rules"]]')

    # Terminal action.
    if [[ "$default_action" == "deny" ]]; then
        iptables -A "$chain" -j DROP
    else
        iptables -A "$chain" -j ACCEPT
    fi

    # Hook into OUTPUT for that uid.
    iptables -D OUTPUT -m owner --uid-owner "$uid" -j "$chain" 2>/dev/null || true
    iptables -I OUTPUT -m owner --uid-owner "$uid" -j "$chain"

    echo "Installed chain $chain → tenant=$tid uid=$uid default=$default_action"
}

if [[ "$ALL" -eq 1 ]]; then
    # Iterate every tenant policy with a non-empty allow-list.
    "$PYTHON_BIN" -m backend.tenant_egress dump-policies | \
        "$PYTHON_BIN" -c '
import json, sys
for p in json.load(sys.stdin):
    if p["allowed_hosts"] or p["allowed_cidrs"] or p["default_action"] == "allow":
        print(p["tenant_id"])
' | while read -r tid; do
        # Operator must supply uid via env table; fall back to OMNISIGHT_DEFAULT_SANDBOX_UID.
        env_var="OMNISIGHT_SANDBOX_UID_$(echo "$tid" | tr '[:lower:]-' '[:upper:]_')"
        uid="${!env_var:-${OMNISIGHT_DEFAULT_SANDBOX_UID:-}}"
        if [[ -z "$uid" ]]; then
            echo "no uid configured for tenant=$tid (set $env_var or OMNISIGHT_DEFAULT_SANDBOX_UID)" >&2
            continue
        fi
        apply_one "$tid" "$uid"
    done
    exit 0
fi

apply_one "$TENANT" "$UID_OWNER"
