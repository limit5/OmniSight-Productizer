#!/usr/bin/env bash
# OP-2734 — daily retention for unbounded log sinks.
#
# Two mechanisms, because the sinks have two shapes:
#   * append-mode files written by long-running units -> logrotate copytruncate
#   * the coordinator's decision-log, a directory of dated day-files -> age prune
#
# The decision-log keeps DECISION_LOG_KEEP_DAYS (14) days. ADR-0021 §9 L6 replays
# only the last 24h, so 14 days is well clear of what recovery needs while
# bounding a directory that reached 1.4 GB. One coordinator cold start alone
# writes ~8.7 MB of coordinator_source_event records.
set -Eeuo pipefail

CONF="${OMNISIGHT_LOGROTATE_CONF:-${HOME:-}/.config/omnisight/logrotate.conf}"
STATE="${OMNISIGHT_LOGROTATE_STATE:-${HOME:-}/.local/state/omnisight-logrotate.status}"
DECISION_LOG="${OMNISIGHT_DECISION_LOG_DIR:-${HOME:-}/.config/omnisight/coordinator/decision-log}"
KEEP_DAYS="${DECISION_LOG_KEEP_DAYS:-14}"

log() { printf '  %s\n' "$*"; }

[[ -r "$CONF" ]] || { printf '  [FAIL] logrotate config unreadable: %s\n' "$CONF" >&2; exit 1; }
mkdir -p "$(dirname "$STATE")"

BEFORE="$(df --output=used -k / | tail -1)"
logrotate --state "$STATE" "$CONF"
log "logrotate ok (state: $STATE)"

if [[ -d "$DECISION_LOG" ]]; then
  n="$(find "$DECISION_LOG" -maxdepth 1 -type f -name '*.jsonl' -mtime "+$KEEP_DAYS" -print -delete | wc -l)"
  log "decision-log: pruned $n file(s) older than ${KEEP_DAYS}d; $(du -sh "$DECISION_LOG" | cut -f1) remains"
fi

AFTER="$(df --output=used -k / | tail -1)"
log "reclaimed $(( (BEFORE - AFTER) / 1024 )) MB"
