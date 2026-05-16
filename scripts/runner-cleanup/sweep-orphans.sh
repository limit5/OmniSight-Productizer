#!/bin/bash
# OP-1139 / Sprint Boreas-C4 — Orphan ephemeral-workspace sweeper.
#
# The OP-1137 ephemeral wrapper (Boreas-C2) cleans up its workspace
# via `trap EXIT INT TERM` on every cycle exit. But SIGKILL of the
# wrapper itself bypasses traps and leaves the workspace orphaned.
# This script sweeps any orphan workspaces older than the configured
# threshold (default 4 hours — comfortably longer than a normal cycle's
# 30-minute timeout, so we never delete an active workspace).
#
# Usage (one-shot):
#   bash scripts/runner-cleanup/sweep-orphans.sh
#
# Periodic via systemd:
#   deploy/systemd/runner-orphan-sweep.timer (every 30 min)

set -uo pipefail

WORKSPACE_ROOT="${OMNISIGHT_RUNNER_WORKSPACE_ROOT:-/tmp/runner-workspaces}"
MAX_AGE_MIN="${OMNISIGHT_RUNNER_ORPHAN_MAX_AGE_MIN:-240}"

log() { printf '[runner-cleanup/sweep-orphans] %s\n' "$*" >&2; }

if [[ ! -d "$WORKSPACE_ROOT" ]]; then
    log "workspace root $WORKSPACE_ROOT does not exist; nothing to sweep"
    exit 0
fi

# Find orphan workspaces: /tmp/runner-workspaces/<instance>/run-XXXXXX
# older than $MAX_AGE_MIN minutes. -mindepth 2 -maxdepth 2 ensures we
# don't accidentally sweep the per-instance dirs themselves.
swept=0
total=0
total_bytes=0
while IFS= read -r -d '' dir; do
    total=$((total + 1))
    size_kb=$(du -sk "$dir" 2>/dev/null | cut -f1 || echo 0)
    total_bytes=$((total_bytes + size_kb))
    rm -rf "$dir" && swept=$((swept + 1))
done < <(find "$WORKSPACE_ROOT" -mindepth 2 -maxdepth 2 -type d -name 'run-*' -mmin "+$MAX_AGE_MIN" -print0 2>/dev/null)

log "swept $swept orphan workspaces (of $total candidates) freeing ~${total_bytes}KB; max_age_min=$MAX_AGE_MIN"

# Also report current workspace count for observability
current=$(find "$WORKSPACE_ROOT" -mindepth 2 -maxdepth 2 -type d -name 'run-*' 2>/dev/null | wc -l)
log "current live workspaces: $current"
