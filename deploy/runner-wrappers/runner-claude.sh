#!/usr/bin/env bash
set -euo pipefail

INSTANCE_ID="${1:-${OMNISIGHT_RUNNER_INSTANCE_ID:-default}}"
BASE_DIR="${OMNISIGHT_RUNNER_BASE_DIR:-/home/user/work/sora}"
LOG_DIR="${OMNISIGHT_RUNNER_LOG_DIR:-${BASE_DIR}/logs/runner}"
RESTART_SLEEP="${OMNISIGHT_RUNNER_RESTART_SLEEP:-90}"
WORKTREE="${OMNISIGHT_CLAUDE_WORKTREE:-${BASE_DIR}/OmniSight-claude-worktree}"

if [[ "${INSTANCE_ID}" != "default" ]]; then
  WORKTREE="${OMNISIGHT_CLAUDE_WORKTREE:-${BASE_DIR}/OmniSight-claude-${INSTANCE_ID}-worktree}"
fi

mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/claude-bot-${INSTANCE_ID}-$(date -u +%Y%m%dT%H%M%SZ).log"

set -a
. /home/user/.config/omnisight/audit-db.env 2>/dev/null || true
set +a

export OMNISIGHT_RUNNER_CLASS=subscription-claude
export OMNISIGHT_RUNNER_INSTANCE_ID="${INSTANCE_ID}"
export OMNISIGHT_CLAUDE_WORKTREE="${WORKTREE}"

cd "${WORKTREE}"

while true; do
  set +e
  python3 "${WORKTREE}/auto-runner-jira.py" 2>&1 | tee -a "${LOG}"
  rc="${PIPESTATUS[0]}"
  set -e
  echo "[launcher-claude-${INSTANCE_ID}] tick exit rc=${rc}; sleeping ${RESTART_SLEEP}s" | tee -a "${LOG}"
  if [[ "${OMNISIGHT_RUNNER_ONCE:-0}" == "1" ]]; then
    exit "${rc}"
  fi
  sleep "${RESTART_SLEEP}"
done
