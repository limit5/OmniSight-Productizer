#!/bin/bash
# OP-1139 / Sprint Boreas-C4 — Disk-usage check for runner workspaces.
#
# Exits non-zero + alerts when the filesystem hosting
# $OMNISIGHT_RUNNER_WORKSPACE_ROOT crosses the threshold. Designed to
# run from cron / smoke-test cron (OP-1132 / Boreas-A7), feeding the
# operator_notifier pipeline.
#
# Exits:
#   0 — disk usage under threshold
#   2 — over threshold (alert)
#
# Output single-line JSON for log consumers.

set -uo pipefail

WORKSPACE_ROOT="${OMNISIGHT_RUNNER_WORKSPACE_ROOT:-/tmp/runner-workspaces}"
THRESHOLD_PCT="${OMNISIGHT_RUNNER_DISK_THRESHOLD_PCT:-80}"

# Filesystem hosting workspace root
mount_path=$(df -P "$WORKSPACE_ROOT" 2>/dev/null | tail -1)
if [[ -z "$mount_path" ]]; then
    printf '{"status": "missing", "message": "cannot stat %s"}\n' "$WORKSPACE_ROOT"
    exit 2
fi

# df -P columns: Filesystem 1024-blocks Used Available Capacity MountedOn
used_pct=$(echo "$mount_path" | awk '{print $5}' | tr -d '%')
mount_on=$(echo "$mount_path" | awk '{print $NF}')

if (( used_pct >= THRESHOLD_PCT )); then
    printf '{"status": "over_threshold", "used_pct": %d, "threshold_pct": %d, "mount": "%s", "message": "disk pressure"}\n' \
        "$used_pct" "$THRESHOLD_PCT" "$mount_on"
    exit 2
fi

printf '{"status": "ok", "used_pct": %d, "threshold_pct": %d, "mount": "%s", "message": "ok"}\n' \
    "$used_pct" "$THRESHOLD_PCT" "$mount_on"
